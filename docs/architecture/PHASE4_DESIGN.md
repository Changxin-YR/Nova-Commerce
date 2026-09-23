# Phase 4 design freeze — Cart + Pricing + Order

> Internal implementation contract for Phase 4. The public wire contract is
> `docs/architecture/API_CONTRACT.md` §14; this file freezes the *code* interfaces so
> several people can implement in parallel without guessing at each other's names.
>
> Status: FROZEN for the duration of Phase 4. If reality contradicts something here,
> collect the evidence, say so, and change this file in the same commit as the code —
> do not implement a second opinion.

## 1. Scope (and what is deliberately out)

In scope:

* `app/modules/pricing/` — the single price authority (§37), pure and DB-free.
* `app/modules/order/` — orders, order items, status logs, the order workflow, reads.
* `app/shared/db/models/idempotency.py` — `idempotency_records` (§48), shared because
  payments (Phase 5) will write the same table.
* One Alembic migration creating the four tables, with CHECK constraints hand-verified
  in `information_schema` (the §6 Alembic trap).
* `POST /orders/preview`, `POST /orders`, `GET /orders`, `GET /orders/{order_no}`,
  `POST /orders/{order_no}/cancel`, `POST /orders/{order_no}/confirm-receipt`,
  `GET /orders/admin`, `GET /orders/admin/{order_no}`.
* FG-10: workflow tests incl. the snapshot-immutability test, on real MySQL.

Out of scope, deliberately:

* No cart table and no `/cart` endpoints: §29/§105 make the cart a client-side
  selection plus a server preview. `app/modules/cart` stays absent.
* No promotion/coupon **tables or resolution** (Phase 6). Pricing implements the
  arithmetic against the shapes frozen in API_CONTRACT §13.1/§13.3, and the live order
  path passes no rules yet.
* No payment write path (Phase 5). `PENDING_PAYMENT → PROCESSING` has no HTTP writer.
* No outbox row (Phase 6). The workflow leaves a single, named seam where it will be
  emitted.
* No frontend edits. The consumer paths in `frontend/src/api/endpoints.ts` use an
  older `/orders/orders/*` shape; the frozen paths are the ones in §14 and the frontend
  migrates in Phase 7 integration.

## 2. Module layout

```
app/modules/pricing/
    __init__.py
    enums.py            # PromotionType, CouponType (frozen vocabularies)
    value_objects.py    # PricedLine, ItemPrice, DiscountAllocation, CartPrice,
                        # PromotionRule, CouponRule, PriceSnapshot
    allocation.py       # allocate_pro_rata()
    shipping.py         # ShippingPolicy protocol + FreeShippingPolicy
    service.py          # PricingService — the six frozen methods
    errors.py           # PricingInvariantError (internal, never a wire code)

app/modules/order/
    __init__.py
    enums.py            # OrderStatus, PaymentStatus, FulfillmentStatus,
                        # AfterSaleStatus + ORDER_STATUS_TRANSITIONS
    models.py           # Order, OrderItem, OrderStatusLog
    repository.py       # OrderRepository, IdempotencyRepository
    schemas.py          # Pydantic request/response models (frozen shapes)
    serializers.py      # to_summary(order), to_detail(order, fulfillments=())
    state_machine.py    # pure transition guard
    service.py          # OrderService — preview/create/cancel/confirm/reads
    workflow.py         # CreateOrderWorkflow — the transaction body
    api/__init__.py
    api/customer.py     # frozen consumer paths
    api/admin.py        # /orders/admin*

app/shared/db/models/
    __init__.py
    idempotency.py      # IdempotencyRecord (table: idempotency_records)
```

## 3. Enums (`app/modules/order/enums.py`)

```python
class OrderStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"

class PaymentStatus(StrEnum):
    UNPAID, PAYING, PAID, PARTIAL_REFUNDED, REFUNDED

class FulfillmentStatus(StrEnum):
    UNFULFILLED, PARTIAL_SHIPPED, SHIPPED, DELIVERED

class AfterSaleStatus(StrEnum):
    NONE, PROCESSING, PARTIAL_REFUNDED, REFUNDED
```

Also export `ORDER_STATUSES`, `PAYMENT_STATUSES`, `FULFILLMENT_STATUSES`,
`AFTER_SALE_STATUSES` tuples, and:

```python
ORDER_STATUS_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: {PROCESSING, CANCELLED, CLOSED},
    OrderStatus.PROCESSING: {COMPLETED, CLOSED},
    OrderStatus.COMPLETED: {CLOSED},
    OrderStatus.CANCELLED: set(),
    OrderStatus.CLOSED: set(),
}
```

The transition table is the only place order statuses move. Payment/fulfillment/after-sale
statuses are **not** in it and are never derived from `order_status` (§31).

## 4. Tables

All money columns are `MoneyMinor` (signed BIGINT — the §6 MySQL mixed-signedness trap:
CHECK arithmetic must be all-signed). All timestamps `DateTimeMS`. Status columns are
`VARCHAR` + Python enum vocabulary, with `CHECK (col IN (...))` constraints whose names
are the ones below (hand-written in the migration — Alembic does not diff CHECKs).

### 4.1 `orders`

| column | type | notes |
|---|---|---|
| id | PkMixin | |
| merchant_id | MerchantScopedMixin | from the SKU's merchant |
| user_id | BIGINT UNSIGNED FK users.id RESTRICT, not null, indexed | the buyer |
| order_no | VARCHAR(32) UNIQUE(merchant_id, order_no) | `NV<YYYYMMDD><6+ digits>` |
| client_request_id | VARCHAR(64) not null | |
| request_hash | VARCHAR(64) not null | sha256 of the canonical business inputs |
| order_status / payment_status / fulfillment_status / after_sale_status | VARCHAR(32) | defaults per §14.5 |
| original_amount / promotion_discount_amount / coupon_discount_amount / shipping_amount / payable_amount / paid_amount / refunded_amount | MoneyMinor not null default 0 | CHECKs below |
| coupon_id | BIGINT UNSIGNED NULL, indexed, **no FK yet** | Phase 6 adds the FK |
| address_id | BIGINT UNSIGNED NULL FK user_addresses.id RESTRICT | snapshot source |
| receiver_name / receiver_phone | VARCHAR(64)/(32) not null | raw; masked on read |
| address_snapshot | JSON not null | province/city/district/detail/postal_code |
| remark | VARCHAR(500) null | |
| cancel_reason | VARCHAR(500) null | |
| item_count | INT not null | total units, snapshot (§11 addendum) |
| first_item_name | VARCHAR(200) not null | snapshot, truncated |
| expires_at | DateTimeMS null | created + `ORDER_PAYMENT_TIMEOUT_MINUTES` |
| paid_at / completed_at / cancelled_at / closed_at | DateTimeMS null | |
| version | VersionMixin | |
| created_at / updated_at | TimestampMixin | |

Indexes: `(merchant_id, order_status)`, `(user_id, created_at)`,
`(merchant_id, created_at)`, UNIQUE `(user_id, client_request_id)`.

CHECK constraint names/expressions:

* `ck_orders_amounts_non_negative`: every amount column `>= 0`.
* `ck_orders_payable_consistent`:
  `payable_amount = original_amount - promotion_discount_amount - coupon_discount_amount + shipping_amount`
* `ck_orders_status_valid`: `order_status IN ('PENDING_PAYMENT','PROCESSING','COMPLETED','CANCELLED','CLOSED')`
* `ck_orders_payment_status_valid`, `ck_orders_fulfillment_status_valid`,
  `ck_orders_after_sale_status_valid` with the §3 vocabularies.

### 4.2 `order_items`

| column | type | notes |
|---|---|---|
| id | PkMixin | |
| order_id | FK orders.id RESTRICT, indexed | |
| warehouse_id | FK warehouses.id RESTRICT | the row that was locked |
| product_id / sku_id | FK RESTRICT | identifiers only; names are snapshots |
| product_name / sku_name | VARCHAR(200) | **snapshot** (INV-014) |
| image_object_key / image_url | VARCHAR(512) null | snapshot |
| sku_snapshot | JSON null | the SKU attribute snapshot at purchase |
| unit_price | MoneyMinor | **snapshot** |
| quantity | Int | CHECK `> 0` |
| original_amount / promotion_discount_amount / coupon_discount_amount / allocated_discount_amount / payable_amount / refunded_amount | MoneyMinor | |
| after_sale_status | VARCHAR(32) default NONE | |
| created_at / updated_at | TimestampMixin | |

Index/unique: UNIQUE `(order_id, sku_id)` (duplicate SKUs are merged before pricing).

CHECK constraint names/expressions:

* `ck_order_items_quantity_positive`: `quantity > 0`
* `ck_order_items_amounts_non_negative`: amounts `>= 0`
* `ck_order_items_allocated_consistent`:
  `allocated_discount_amount = promotion_discount_amount + coupon_discount_amount`
* `ck_order_items_payable_consistent`:
  `payable_amount = original_amount - allocated_discount_amount`
* `ck_order_items_after_sale_status_valid`: vocabulary.

The order-level assertion (INV-006) is *not* a CHECK — it spans rows — and is executed
inside the creating transaction after flush: `sum(item.payable_amount) == order.payable_amount`.

### 4.3 `order_status_logs`

| column | type |
|---|---|
| id | PkMixin |
| order_id | FK orders.id RESTRICT, indexed |
| order_no | VARCHAR(32) |
| from_status | VARCHAR(32) null |
| to_status | VARCHAR(32) not null |
| reason | VARCHAR(500) null |
| operator_type | VARCHAR(32) (`SYSTEM`/`CUSTOMER`/`STAFF`/`AGENT`/`MCP`/`WORKER`) |
| operator_id | BIGINT UNSIGNED null |
| trace_id | VARCHAR(64) null |
| created_at | TimestampMixin |

Append-only: no update/delete methods anywhere, and records only top-level
`order_status` transitions (§36). One log row on create (`None → PENDING_PAYMENT`), one
per later transition.

### 4.4 `idempotency_records` (shared)

| column | type | notes |
|---|---|---|
| id | PkMixin | |
| scope | VARCHAR(64) | Phase 4 uses `order:create` |
| idempotency_key | VARCHAR(128) | the header value |
| request_hash | VARCHAR(64) | sha256 of canonical inputs |
| status | VARCHAR(16) | `IN_PROGRESS` / `COMPLETED` / `FAILED` |
| response_code | Int null | business code |
| response_snapshot | JSON null | **non-sensitive summary only** (§48) |
| resource_type / resource_id | VARCHAR(32) / BIGINT UNSIGNED null | |
| expires_at | DateTimeMS null, indexed | retention, not a lock TTL |
| created_at / updated_at | TimestampMixin | |

UNIQUE `(scope, idempotency_key)`. The record is inserted and finalised **inside the
order transaction**, so a rollback leaves no key and a genuine retry is allowed.

## 5. Pricing value objects

```python
@dataclass(frozen=True, slots=True)
class PricedLine:
    sku_id: int
    product_id: int
    product_name: str
    sku_name: str
    image_object_key: str | None
    image_url: str | None
    sku_snapshot: dict | None
    unit_price: int
    quantity: int

@dataclass(frozen=True, slots=True)
class ItemPrice:
    line: PricedLine
    original_amount: int

@dataclass(frozen=True, slots=True)
class DiscountAllocation:
    sku_id: int
    amount: int

@dataclass(frozen=True, slots=True)
class CartPrice:
    items: tuple[ItemPrice, ...]
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    promotion_allocations: tuple[DiscountAllocation, ...]
    coupon_allocations: tuple[DiscountAllocation, ...]
    warnings: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class PromotionRule:
    promotion_id: int
    promotion_type: PromotionType          # DIRECT_DISCOUNT | PERCENT_DISCOUNT | FULL_REDUCTION
    threshold_amount: int = 0              # FULL_REDUCTION
    discount_amount: int = 0               # DIRECT_DISCOUNT, minor units PER UNIT
    discount_bps: int = 0                  # PERCENT_DISCOUNT
    reduction_amount: int = 0              # FULL_REDUCTION
    max_discount_amount: int | None = None
    applicable_sku_ids: frozenset[int] | None = None   # None = all lines

@dataclass(frozen=True, slots=True)
class CouponRule:
    coupon_id: int
    coupon_type: CouponType                # FIXED_AMOUNT | PERCENT_DISCOUNT
    threshold_amount: int = 0
    face_value_amount: int = 0
    discount_bps: int = 0
    max_discount_amount: int | None = None
    applicable_sku_ids: frozenset[int] | None = None

@dataclass(frozen=True, slots=True)
class PriceSnapshot:  # what build_price_snapshot returns; persisted by the workflow
    items: tuple[ItemPrice, ...]
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    promotion_allocations: tuple[DiscountAllocation, ...]
    coupon_allocations: tuple[DiscountAllocation, ...]
    promotion_ids: tuple[int, ...]
    coupon_ids: tuple[int, ...]
    shipping_policy: str
```

`PromotionRule`/`CouponRule` are the **resolved view** of API_CONTRACT §13.1/§13.3
`rule_config`/`applicable_scope`. Phase 6's marketing module builds them; Phase 4 never
imports marketing models.

## 6. Pricing algorithm (the one authority)

`PricingService` is stateless; every method is pure. Method signatures:

```python
class PricingService:
    def calculate_item_price(self, line: PricedLine) -> ItemPrice
    def calculate_cart_price(self, lines, *, promotion=None, coupon=None,
                             shipping_policy=None) -> CartPrice
    def apply_promotion(self, cart: CartPrice, promotion: PromotionRule) -> CartPrice
    def apply_coupon(self, cart: CartPrice, coupon: CouponRule) -> CartPrice
    def calculate_shipping(self, cart: CartPrice, policy: ShippingPolicy | None = None) -> int
    def build_price_snapshot(self, cart: CartPrice, *, promotion_ids=(), coupon_ids=(),
                             shipping_policy="FREE") -> PriceSnapshot
```

Steps, in this order:

1. `original_amount = unit_price * quantity` per line, after merging duplicate SKUs.
2. `apply_promotion` on the eligible lines:
   * DIRECT_DISCOUNT: `discount_amount × eligible_units`, capped at eligible original total.
   * PERCENT_DISCOUNT: `floor(eligible_original × discount_bps / 10000)`, capped by
     `max_discount_amount` and by the eligible original total.
   * FULL_REDUCTION: if `eligible_original >= threshold_amount` →
     `min(reduction_amount, max_discount_amount or ∞, eligible_original)`, else 0.
   * Allocate the order-level discount pro-rata over eligible lines by their original
     amount, remainder to the **last eligible line**.
3. `apply_coupon` on the eligible lines, against
   `eligible_original − promotion_allocated_to_eligible`:
   * FIXED_AMOUNT: `min(face_value_amount, remaining)`, threshold on eligible original.
   * PERCENT_DISCOUNT: `floor(remaining × discount_bps / 10000)`, capped by remaining
     and `max_discount_amount`.
   * Same pro-rata allocation over eligible lines by their remaining amount, remainder
     to the last eligible line.
4. `calculate_shipping`: V1 `FreeShippingPolicy` returns 0. Any non-zero result makes
   `build_price_snapshot` raise `PricingInvariantError`, because a shipping charge has no
   per-item home and would break INV-006 by construction (§14.4).
5. `payable = original − promotion − coupon + shipping`.

`allocate_pro_rata(total, weights)` must guarantee `sum(result) == total`, `total == 0`
→ all zeros, and remainder to the last non-... (last element of the sequence, even when
it is zero-weight — deterministic beats clever). It is unit-tested with weights that do
not divide evenly, including `(1,1,1)`/100 and `(2,2,3)`-style cases.

`build_price_snapshot` asserts `sum(item.payable_amount) == cart.payable_amount` and
raises `PricingInvariantError` if not. INV-006 is enforced by the algorithm *and* this
assertion, never by hope.

## 7. Workflow

```python
@dataclass(slots=True)
class CreateOrderResult:
    order: Order
    replayed: bool

class CreateOrderWorkflow:
    def __init__(self, session: Session, pricing: PricingService | None = None) -> None
    def execute(
        self,
        *,
        principal,                       # identity Principal
        items: Sequence[OrderLineInput], # sku_id + quantity, already deduped
        address_id: int,
        client_request_id: str,
        idempotency_key: str,
        coupon_id: int | None = None,
        remark: str | None = None,
        pricing_rules: PricingRules | None = None,  # INTERNAL seam, tests/Phase 6 only
        trace_id: str | None = None,
    ) -> CreateOrderResult
```

`PricingRules` is a tiny dataclass holding `promotion: PromotionRule | None` and
`coupon: CouponRule | None`. The HTTP layer always passes `None`; tests use it to prove
the allocation arithmetic end-to-end through the real transaction. This seam is why
Phase 4 can satisfy FG-10 without marketing tables.

Transaction body, in order (the §27 rule generalised: **decide inside the lock**):

1. Resolve/lock the idempotency record `(order:create, key)`.
   * existing COMPLETED + same hash → load the order, return `replayed=True`;
   * existing COMPLETED + different hash → `IdempotencyPayloadMismatchError` (10011);
   * missing → insert IN_PROGRESS (the unique index serialises concurrent callers).
2. Validate + snapshot the address (`AddressService.get` — own address only).
3. Load SKUs + product + primary image; refuse non-ACTIVE SKUs / non-PUBLISHED products.
4. Price via `PricingService` (no client numbers anywhere).
5. `SELECT ... FOR UPDATE` every inventory row, **sorted by sku_id ascending** (deadlock
   avoidance), via `InventoryService.reserve` with movement keys
   `order-lock:{user_id}:{client_request_id}:{sku_id}`.
6. Insert the order (temporary unique `order_no`, flush, then stamp
   `NV{YYYYMMDD}{id:06d}`), insert items, insert the create status log.
7. Assert INV-006 in-transaction; assert `item_count`/`first_item_name`.
8. Mark the idempotency record COMPLETED with a **non-sensitive** response snapshot
   (`order_no`, `payable_amount`, `created_at` only).
9. Single commit owned by this workflow; on any exception roll back everything (stock
   included). The outbox emission point is marked with a comment (Phase 6 adds the row
   here, in the same transaction).

The `client_request_id` unique constraint is the second guard: on IntegrityError,
rollback, look the order up by `(user_id, client_request_id)`, and return it replayed if
its stored `request_hash` matches, else raise 10011.

## 8. Service surface (`OrderService`)

```python
class OrderService:
    def preview(self, *, principal, items, address_id=None, coupon_id=None) -> CartPrice
    def create_order(self, *, principal, items, address_id, client_request_id,
                     idempotency_key, coupon_id=None, remark=None) -> CreateOrderResult
    def cancel(self, *, principal, order_no, reason=None) -> Order
    def confirm_receipt(self, *, principal, order_no) -> Order
    def get_customer_order(self, *, principal, order_no) -> Order
    def list_customer_orders(self, *, principal, order_status=None, page=1, page_size=20)
    def get_admin_order(self, *, principal, order_no) -> Order
    def list_admin_orders(self, *, principal, order_status=None, payment_status=None,
                          fulfillment_status=None, order_no=None, page=1, page_size=20)
```

* Ownership: customer reads/cancel/confirm are `user_id == principal.user_id`; a stranger's
  order is `ORDER_NOT_FOUND (50003)`, never 403.
* Console reads require `ConsolePrincipal` + `ORDER_READ`; merchant scope is enforced in
  the query.
* `cancel` locks the order `FOR UPDATE`, guards the transition, releases each item's
  reservation with `ORDER_RELEASE` (`order-release:{order_no}:{sku_id}`), writes
  `cancel_reason` + log, commits.
* `confirm_receipt` locks, guards `PROCESSING → COMPLETED`, stamps `completed_at`, logs,
  commits. It does not touch fulfillment status.
* `coupon_id` non-null with no resolver wired → `CouponNotFoundError (90004)`.

## 9. API layer

Paths are the ones frozen in `PROJECT_BASELINE.yaml` §96 and API_CONTRACT §14 — **not**
the frontend's current `/orders/orders/*` guesses:

| handler file | route | full path |
|---|---|---|
| `api/customer.py` | `POST /preview` | `/api/v1/orders/preview` |
| `api/customer.py` | `POST ""` | `/api/v1/orders` |
| `api/customer.py` | `GET ""` | `/api/v1/orders` |
| `api/customer.py` | `GET /{order_no}` | `/api/v1/orders/{order_no}` |
| `api/customer.py` | `POST /{order_no}/cancel` | `/api/v1/orders/{order_no}/cancel` |
| `api/customer.py` | `POST /{order_no}/confirm-receipt` | `/api/v1/orders/{order_no}/confirm-receipt` |
| `api/admin.py` | `GET /admin` | `/api/v1/orders/admin` |
| `api/admin.py` | `GET /admin/{order_no}` | `/api/v1/orders/admin/{order_no}` |

`app/api/v1/router.py` must register the order module's `admin` router **before**
`customer`, otherwise `GET /orders/admin` is captured by `GET /orders/{order_no}`.
Put a comment there saying exactly that, so it is not "tidied" back.

Every handler is thin: parse → `get_current_principal`/`ConsolePrincipal` → one service
call → `envelope(...)`. `Idempotency-Key` is a required `Header` on create, producing
`IdempotencyKeyRequiredError` (10010) when missing.

## 10. Settings / redaction touched by this phase

* `Settings.ORDER_PAYMENT_TIMEOUT_MINUTES: int = 15` and `Settings.ORDER_NO_PREFIX: str = "NV"`,
  plus the matching `.env.example` keys (the settings contract test fails otherwise).
* `app/core/redaction.py`: add `mask_name` ("张*", "张**"; empty stays empty) because §94
  masks the receiver and the contract's example shows a masked name. Unit test added.

## 11. Test plan (what "done" means)

Unit (no DB):

* allocation: sum-exact property over awkward weight sets; remainder to last; zero total.
* pricing: each promotion type, caps, thresholds, ineligible-line scoping, coupon after
  promotion, `payable >= 0`, INV-006 exactness, non-zero shipping raises.
* state machine: every legal transition and a representative set of illegal ones;
  fulfillment/payment/after-sale statuses are not inputs.
* redaction `mask_name`.

Integration (real MySQL, marked `integration`), the FG-10 core:

1. create → order persisted, amounts recomputed from SKU price, stock reserved
   (`available--`, `locked++`, one `ORDER_LOCK` movement), status log written.
2. **snapshot immutability**: after create, update product name / sku name / price /
   image / attribute snapshot, re-read the order — every field identical (INV-014).
3. **INV-006 through the real transaction** with a promotion and a coupon applied via the
   internal `pricing_rules` seam (weights that do not divide evenly).
4. idempotency: same key+body replay returns the same order with `replayed=True` and
   creates exactly one order row; different body → 10011; same `client_request_id`
   different key → same order; missing key → 10010.
5. cancel: `PENDING_PAYMENT → CANCELLED`, stock released, movement appended, log
   appended, `cancel_reason` recorded; second cancel → 50010; cancelling PROCESSING →
   50010.
6. confirm-receipt: `PROCESSING → COMPLETED`; from PENDING_PAYMENT → 50011; fulfillment
   status untouched.
7. reads: consumer sees only own orders (stranger gets 50003); admin list filters and
   paged meta; `item_count`/`first_item_name`/`refundable_amount`/masked receiver.
8. HTTP smoke: the eight frozen paths are registered; create without a token is 401;
   one authenticated create→get→cancel round trip (token minted via `AuthService`).

Concurrency (real MySQL, marked `concurrency`): N threads, same `Idempotency-Key` →
exactly one order row and no extra stock movement.

FG-10 evidence: `scripts/emit_fg10_evidence.py` runs the integration file and writes
`artifacts/evidence/integration/fg10_workflow.xml` (JUnit) plus
`fg10_workflow.json` (the same assertion/verdict shape as the FG-09 report).

## 12. File ownership (avoid two people editing one file)

| owner | files |
|---|---|
| pricing-author | `backend/app/modules/pricing/**`, `backend/tests/unit/modules/pricing/**` |
| order-data | `backend/app/modules/order/{__init__,enums,models,repository}.py`, `backend/app/shared/db/models/**`, `backend/migrations/versions/*phase4*` |
| order-flow | `backend/app/modules/order/{schemas,serializers,state_machine,service,workflow}.py`, `backend/app/modules/order/api/**`, `backend/tests/unit/modules/order/**`, `backend/tests/integration/order/**` |
| captain | contract §14 (done), this file, `core/config.py` + `.env.example`, `core/redaction.py`, `api/v1/router.py` wiring, FG-10 evidence, HANDOFF update, commits |
| verifier | `scripts/emit_fg10_evidence.py`, read-only elsewhere; reports defects to owners |

Anyone who finds a needed change in a file they do not own messages the owner (or the
captain) instead of editing it.
