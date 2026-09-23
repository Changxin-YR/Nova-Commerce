# Order Workflow

> Spec sections 30–38, 41, 48, 96. Wire contract: `docs/architecture/API_CONTRACT.md`
> §6 and §14. Implementation freeze: `docs/architecture/PHASE4_DESIGN.md`.
>
> Status: Phase 4 (Cart + Pricing + Order). Phase 5 extends this document with
> payment, fulfillment and refund transitions; nothing here may be re-litigated
> silently — if reality contradicts it, change this file in the same commit.

## 1. The four status axes

An order carries four independent status fields, and **no axis is ever derived
from another** (spec §31):

| Field | Vocabulary | Written by |
|---|---|---|
| `order_status` | `PENDING_PAYMENT`, `PROCESSING`, `COMPLETED`, `CANCELLED`, `CLOSED` | the order state machine only |
| `payment_status` | `UNPAID`, `PAYING`, `PAID`, `PARTIAL_REFUNDED`, `REFUNDED` | payment/refund workflows (Phase 5) |
| `fulfillment_status` | `UNFULFILLED`, `PARTIAL_SHIPPED`, `SHIPPED`, `DELIVERED` | shipping workflow (Phase 5) |
| `after_sale_status` | `NONE`, `PROCESSING`, `PARTIAL_REFUNDED`, `REFUNDED` | after-sale/refund workflows (Phase 5) |

The distinction that matters most: **shipping never changes `order_status`**. An
order that has been shipped is still `PROCESSING` until the customer confirms
receipt. A UI that wants "can this be shipped?" must read
`fulfillment_status`; a UI that reads `order_status` will show a shipped order as
unshipped, which is exactly the defect the frontend's availability tests pin
down.

## 2. `order_status` transitions

```
                  create
                    │
                    ▼
            PENDING_PAYMENT ──────────► CANCELLED   (cancel endpoint; releases stock)
              │        │
              │        └──────────────► CLOSED      (expiry reconciliation, Phase 6)
              │ payment success
              ▼
           PROCESSING ────────────────► COMPLETED   (confirm-receipt)
              │
              └───────────────────────► CLOSED      (Phase 5/6, after-sale closure)

           CANCELLED, CLOSED are terminal.
```

`ORDER_STATUS_TRANSITIONS` in `app/modules/order/enums.py` is the single source
of truth; `OrderStateMachine.assert_transition` is the only writer guard. Phase 4
implements the create → `PENDING_PAYMENT`, `PENDING_PAYMENT → CANCELLED` and
`PROCESSING → COMPLETED` edges. The `PENDING_PAYMENT → PROCESSING` edge has no
HTTP writer in Phase 4 — it belongs to `PaymentSuccessWorkflow` (Phase 5) — and
`CLOSED` belongs to the expiry reconciliation (Phase 6).

Every transition appends exactly one `order_status_logs` row. That table is
append-only and records **only** top-level order status; payment/fulfillment
changes belong to their own tables in their own phases (spec §36).

## 3. `CreateOrderWorkflow` (`POST /api/v1/orders`)

One database transaction owned by `CreateOrderWorkflow.execute`; nothing inside
it commits except the workflow itself.

1. **Idempotency guard.** Resolve `(scope=order:create, Idempotency-Key)`.
   * completed + same request hash → load the order, return it as a replay;
   * completed + different hash → `10011`;
   * absent → insert `IN_PROGRESS` (the unique index is the serialisation point).
2. **Address snapshot.** `AddressService.get(user_id, address_id)` — the caller
   must own it, and a missing/foreign address is a 404, never a 403 (IDOR).
   Province/city/district/detail are copied into `orders.address_snapshot`.
3. **Load the SKUs** (product, primary image, attribute snapshot). Only `ACTIVE`
   SKUs of `PUBLISHED` products are sellable.
4. **Price.** `PricingService.calculate_cart_price` is the only price authority
   (spec §37). The request contributes SKU ids and quantities and nothing else:
   no unit price, no discount, no payable amount is accepted from the client
   (spec §38).
5. **Insert the order** with its amounts and snapshots, flush, stamp
   `order_no = NV<YYYYMMDD><id:06d>`. Inserting before the reservation gives
   every stock movement a real `reference_id = order.id` (INV-007).
6. **Reserve stock**, SKU ids in ascending order (deadlock avoidance), through
   `InventoryService.reserve` with `reference_type=ORDER`,
   `reference_id=order.id` and idempotency key
   `order-lock:{user_id}:{client_request_id}:{sku_id}`. The row lock is
   `SELECT ... FOR UPDATE`; the decision is made **after** the lock, never
   before (spec §27).
7. **Persist items and the creation log**, then assert
   `SUM(order_items.payable_amount) == orders.payable_amount` (INV-006) and that
   the stored `item_count`/`first_item_name` agree with the lines.
8. **Finalise the idempotency record** with a non-sensitive snapshot
   (`order_no`, `payable_amount`, `created_at` only — spec §48 forbids PII here).
9. **Commit once.** Any exception rolls back the order, the items, the log, the
   stock reservation and the idempotency claim together.

The Phase 6 outbox row is emitted at step 8/9, in this same transaction; a
comment marks the spot so it cannot be added in a second transaction later
(spec §49).

## 4. Idempotency

Two guards, both in the database, because an application-level "check then act"
is not atomic:

| Guard | Where | Catches |
|---|---|---|
| `UNIQUE (scope, idempotency_key)` on `idempotency_records` | the header | a retried request with the same key |
| `UNIQUE (user_id, client_request_id)` on `orders` | the body field | a retried request whose header was lost |

Semantics (API contract §14.3):

* missing `Idempotency-Key` → `10010`; the create endpoint refuses to run;
* same key, same body → 200 with the original order, `replayed` internally; no
  second order, no second stock movement;
* same key, different body → `10011`;
* different key, same `client_request_id`, same body → the original order;
* different key, same `client_request_id`, different body → `10011`.

The idempotency record lives **inside the order transaction**, so a rollback
leaves no key behind and a genuine retry after a real failure is allowed. The
record is not a lock with a TTL: `expires_at` is retention only, and the
`IN_PROGRESS` marker is cleared only by the transaction that set it.

## 5. Money

* Every amount is an **integer in minor units**; no float touches a price.
* `payable_amount = original_amount − promotion_discount_amount − coupon_discount_amount + shipping_amount`.
* Order-level discounts are allocated **pro-rata by each line's original
  amount**, the remainder handed out unit-by-unit from the last line backwards,
  skipping zero-weight lines. For every ordinary cart that is exactly spec §41's
  "the remainder is absorbed by the last item"; the backward walk only diverges
  when a literal last-item remainder would push a line's `payable_amount`
  negative, which the database refuses.
* `SUM(order_items.payable_amount) == orders.payable_amount` (INV-006) is
  enforced three times: by the allocation algorithm, by an in-transaction
  assertion, and per line by `CHECK (payable_amount = original_amount − allocated_discount_amount)`.
* **Shipping is 0 in V1.** `PricingService.calculate_shipping` exists because
  §37 freezes the method; the V1 policy is a free-shipping object, and
  `build_price_snapshot` refuses a non-zero shipping amount because it has no
  per-item home and would break INV-006 by construction. Paid shipping requires
  an explicit decision (allocate it to lines, or amend the invariant) before it
  is switched on.
* Promotion and coupon **rules** are Phase 6's; Phase 4 implements and unit-tests
  the arithmetic against the rule shapes frozen in contract §13.1/§13.3. The
  live create path applies no rules until Phase 6 wires the resolver, and a
  non-null `coupon_id` is refused with `90004` rather than silently ignored.

## 6. The trade snapshot (INV-014)

`order_items` copies `product_name`, `sku_name`, `image_object_key`,
`image_url`, `sku_snapshot`, `unit_price` and every money field at creation.
The read path (`app/modules/order/serializers.py`) performs **no catalogue
lookup at all** — not even to resolve an image URL. A product rename, a price
change, a withdrawn SKU or an edited attribute can therefore never rewrite a
historical order. The `product_id`/`sku_id` columns exist for reporting and
traceability, not for rendering.

`OrderSummary` carries `item_count` and `first_item_name` as stored snapshot
columns, so an order list is one query rather than an N+1 detail fetch, and the
list cannot display something the order did not record.

## 7. Cancel and confirm-receipt

**Cancel** (`POST /api/v1/orders/{order_no}/cancel`, body `{reason?}`):

1. lock the order row `FOR UPDATE`;
2. guard the transition — only `PENDING_PAYMENT → CANCELLED`, otherwise `50010`;
3. release every line's reservation in its own warehouse with
   `ORDER_RELEASE` movements (`order-release:{order_no}:{sku_id}`);
4. write `cancel_reason`, `cancelled_at`, the status log row; commit.

Phase 6 adds coupon release to step 3. Phase 5 decides whether a paid order can
be cancelled through this endpoint or only through a refund workflow; in Phase 4
it cannot.

**Confirm receipt** (`POST /api/v1/orders/{order_no}/confirm-receipt`):

1. lock the order row; guard `PROCESSING → COMPLETED`, otherwise `50011`;
2. stamp `completed_at`, append the status log row; commit.

It never touches `fulfillment_status`: a customer confirming receipt does not
retroactively ship the parcel, and the four axes stay independent.

## 8. Reads, scopes and masking

| Endpoint | Scope |
|---|---|
| `GET /api/v1/orders` | the caller's own orders, paged, filter `order_status` |
| `GET /api/v1/orders/{order_no}` | own order; somebody else's returns `50003`, never 403 |
| `GET /api/v1/orders/admin` | console + `order:read`, merchant-scoped |
| `GET /api/v1/orders/admin/{order_no}` | same |

Lists always return the paged envelope `{items, meta}` (contract §3), never a
bare array, and `meta` is present even for a single page. `receiver_name` and
`receiver_phone` are masked on **both** surfaces (spec §94); `full_address` on
the detail payload is the masked address snapshot, and the client must not try to
un-mask it. `refundable_amount = max(paid_amount − refunded_amount, 0)` is
server-owned because it decides whether a refund control renders (contract §11).

## 9. Error codes used by this workflow

| Code | Name | When |
|---|---|---|
| `50001` | `CART_EMPTY` | preview/create with no lines |
| `50003` | `ORDER_NOT_FOUND` | unknown order, or another user's order |
| `50010` | `ORDER_NOT_CANCELLABLE` | cancel in a state that does not allow it |
| `50011` | `ORDER_NOT_CONFIRMABLE` | confirm-receipt outside `PROCESSING` |
| `40000` | `INSUFFICIENT_STOCK` | any line cannot be reserved |
| `50008` | `ADDRESS_NOT_FOUND` | address absent or not owned by the caller |
| `90004` | `COUPON_NOT_FOUND` | coupon supplied before Phase 6 wires the resolver |
| `10010` | `IDEMPOTENCY_KEY_REQUIRED` | create without the header |
| `10011` | `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD` | same key/request id, different body |
| `10012` | `IDEMPOTENCY_REQUEST_IN_PROGRESS` | a concurrent request holds the key |

## 10. Invariant map

| Invariant | Enforced by | Proven by |
|---|---|---|
| INV-006 order total = sum of lines | allocation algorithm + in-transaction assertion + per-line CHECK | FG-10 order amount invariant test |
| INV-014 snapshots immune to catalogue edits | snapshot columns + catalogue-free read path | FG-10 snapshot mutation test |
| INV-015 same key never duplicates | `idempotency_records` unique + `orders` unique + replay semantics | FG-10 idempotency tests |
| INV-003 same stock never deducted twice | `inventory_movements.idempotency_key` unique (Phase 3) | FG-09, re-run with orders |
| INV-007 every stock change explained | `reference_type`/`reference_id` on every movement | ledger verification test |

## 11. What Phase 5 adds (and inherits)

* `PaymentSuccessWorkflow`: verified callback → `payment_status` `PAID`,
  `PENDING_PAYMENT → PROCESSING`, `ORDER_DEDUCT` movements, fulfillment shell,
  outbox — all idempotent on `UNIQUE(provider, provider_event_id)`.
* Fulfillment: `fulfillments` / `fulfillment_items`, `fulfillment_status`
  transitions, `POST /fulfillments/{id}/ship`; `order_status` untouched.
* After-sales and refunds: `refunded_amount`/`after_sale_status` writers, and the
  refund caps (`refunded ≤ paid`, item refund ≤ item payable).
* **A deletion with an owner:** when `OrderDetail.refundable_amount` starts being
  returned for real, the frontend's `paid − refunded` fallback bridge and its
  test branch must be deleted in the same change. Until then the bridge is the
  only thing keeping refund controls visible on a payload that predates the
  backend field.

* **Single-merchant coupling (do not relax silently):** `_reserve_stock` resolves
  **one** warehouse per order. That is equivalent to resolving per line only because
  `load_priced_lines` refuses a cart spanning merchants, so every line shares one
  `merchant_id` and `get_default(merchant_id=...)` is deterministic. If that guard is
  ever relaxed - a marketplace cart - the resolution must move inside the loop in the
  same change, or every line in the order would be locked against one merchant's
  warehouse. Phase 5 inherits the guard as the enforcement, not a comment.
