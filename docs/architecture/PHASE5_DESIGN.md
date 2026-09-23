# Phase 5 design - Payment + Fulfillment + AfterSales + Refund

> Written by the captain before any code, for the same reason `PHASE4_DESIGN.md`
> was: parallel authors cannot share a file, so they must share a **written**
> interface instead. This document is the contract between the four implementers.
> It is not the API contract (that is `docs/architecture/API_CONTRACT.md`, which
> the captain extends in the same phase) - it is the internal shape: tables,
> enums, workflow order, idempotency keys, error codes and file ownership.
>
> Frozen decisions here come from `PROJECT_BASELINE.yaml` (REQ-PAY-001..006,
> REQ-FUL-001..002, REQ-AFS-001..002, workflow inventory), `ORDER_WORKFLOW.md`
> section 11, `API_CONTRACT.md` sections 4-6 and the HANDOFF section 17.4. If a
> decision here contradicts one of those, **stop and ask the captain** - do not
> pick silently.

## 1. Scope

Phase 5 owns four things and nothing else:

1. **payments** - the money-in record and the provider callback that makes it real.
2. **fulfillments** - the goods-out record; one order may ship several packages.
3. **after_sales** - the customer's *claim* (a business fact).
4. **refunds** - the money-out record (a money fact), with the two caps.

Explicitly out of scope: the transactional outbox table itself (Phase 6 - Phase 5
only preserves the seam), promotions/coupons (Phase 6), analytics (Phase 6),
scheduled reconciliation of expired orders (Phase 6).

## 2. The one rule that shapes everything

**A payment can only become PAID from a verified provider callback, and that
callback is idempotent at the database.** Everything else in the payment domain
is arranged around those two sentences:

* `payment_callbacks` carries `UNIQUE (provider, provider_event_id)`. Two
  deliveries of the same event cannot both insert, so the second one loses at the
  index rather than inside an `if`.
* The callback handler does not make decisions the database can make. It
  *attempts* the insert (never `SELECT`-then-`INSERT`, which is a check-then-act
  race - see `HANDOFF.md` section 6 and `CreateOrderWorkflow`'s module docstring).
* A normal JWT user must never be able to set a payment to success
  (REQ-PAY-005, INV-008). The mock channel is DEV/DEMO-only, uses a separate
  authentication model (a signed provider payload, not a bearer token), and is
  refused outright when `APP_ENV` is not dev/test.

## 3. Module layout (file ownership)

> **The layout below is intent, not mandate.** Where a layer would exist only to
> satisfy this list, it does not get created. Two rulings from the build:
>
> * `fulfillment/workflow.py` does not exist, should not, and has been struck from
>   the layout above. The ship path is `FulfillmentService.ship` plus the pure
>   `compute_fulfillment_status` / `compute_residual` helpers; adding an empty
>   workflow module would be scaffolding with no owner and no caller. It was listed
>   here for symmetry with the other modules, and symmetry is not a reason for a
>   layer to exist. The same correction applies to any docstring that names a
>   `ShipWorkflow`.
> * `payment/api/admin.py` exists because the console needs the payment list the
>   contract froze; a module that only mirrors another module's endpoints does not.
>
> A docstring naming a module that should not exist is worse than no docstring -
> fix the reference, do not create the file.


```
backend/app/modules/payment/
    __init__.py          # public surface: enums, models, service, workflow
    enums.py             # PaymentChannel, PaymentRecordStatus, CallbackProcessStatus
    models.py            # payments, payment_callbacks
    repository.py        # PaymentRepository, PaymentCallbackRepository
    schemas.py           # request/response pydantic models
    serializers.py       # ORM -> frozen wire shapes
    service.py           # use cases: create, read, mock-pay
    workflow.py          # PaymentSuccessWorkflow (the callback transaction body)
    providers.py         # signature verification + non-sensitive payload filtering
    api/
        __init__.py
        customer.py      # /payments/customer/*
        callbacks.py     # /payments/callbacks/*  (provider auth, NOT JWT)
        admin.py         # /payments/admin/*

backend/app/modules/fulfillment/
    __init__.py
    enums.py             # FulfillmentStatus re-export + Carrier vocabulary
    models.py            # fulfillments, fulfillment_items
    repository.py        # FulfillmentRepository
    schemas.py           # ShipFulfillmentRequest (exactly 3 fields)
    serializers.py       # ORM -> API_CONTRACT section 5 Fulfillment shape
    service.py           # create_shell (called by payment), ship, reads
    api/
        __init__.py
        customer.py      # /fulfillments/customer/*
        admin.py         # /fulfillments/admin

backend/app/modules/aftersales/
    __init__.py
    enums.py             # AfterSaleType, AfterSaleClaimStatus, RefundStatus
    models.py            # after_sales, after_sale_items, refunds
    repository.py        # AfterSaleRepository, RefundRepository
    schemas.py
    serializers.py
    service.py           # AfterSaleService: apply/approve/reject/cancel/read
    workflow.py          # RefundWorkflow: the money fact + the two caps
    api/
        __init__.py
        customer.py      # /after-sales/customer/*
        admin.py         # /after-sales/admin/*
        refunds.py       # /after-sales/refunds/* (read-only)
```

`backend/app/api/v1/router.py` is **already wired** for these three module paths
(see the module/path table there: `/payments` with `customer, callbacks, admin`,
`/fulfillments` with `customer, admin`, `/after-sales` with
`customer, admin, refunds`). A missing submodule is skipped defensively, so you
can land your router file whenever it exists. **Do not edit `router.py`** unless
the captain asks: it is a shared file.

## 4. Vocabularies (frozen)

### 4.1 `payments.channel` and `payments.status` - NEW, internal to the payment record

The order's `payment_status` axis (UNPAID/PAYING/PAID/PARTIAL_REFUNDED/REFUNDED)
already exists in `app/modules/order/enums.py` and is **the order's** view. A
payment row is a different object and needs its own status:

```python
class PaymentChannel(StrEnum):
    MOCK = "MOCK"
    ALIPAY = "ALIPAY"
    WECHAT = "WECHAT"

class PaymentRecordStatus(StrEnum):
    INITIATED = "INITIATED"   # created, provider not yet answered
    PAYING = "PAYING"         # handed to the provider
    SUCCESS = "SUCCESS"       # only a verified callback writes this
    FAILED = "FAILED"
    CLOSED = "CLOSED"
    REFUNDED = "REFUNDED"     # fully refunded
    PARTIAL_REFUNDED = "PARTIAL_REFUNDED"

class CallbackProcessStatus(StrEnum):
    RECEIVED = "RECEIVED"     # row inserted, business effect not yet applied
    PROCESSED = "PROCESSED"   # business effect committed
    FAILED = "FAILED"         # signature invalid or processing refused
    IGNORED = "IGNORED"       # a duplicate delivery / unknown payment_no
```

`PaymentChannel` must match the frontend's frozen union in
`frontend/src/types/domain.ts` (`'MOCK' | 'ALIPAY' | 'WECHAT'`). `PAYING` is the
status the *create* endpoint writes, which is what moves the order's axis
`UNPAID -> PAYING`: creating a payment attempt is not a payment, but it is
unambiguously not "unpaid with nothing in flight".

### 4.2 fulfillment

`fulfillments.fulfillment_status` uses the **existing** `FulfillmentStatus`
membership from `app/modules/order/enums.py`
(UNFULFILLED/PARTIAL_SHIPPED/SHIPPED/DELIVERED) - import it, do not re-declare it.
Widening or changing that vocabulary is a migration, and Phase 4 already froze it.

`carrier` is a **CODE** ("SF", "YTO", "JD"), not free text, and is validated
against a small allowlist in the fulfillment enums (the console shows a select).
An unrecognised code is a `ValidationError` rather than a stored arbitrary string.

### 4.3 after-sales

```python
class AfterSaleType(StrEnum):
    REFUND_ONLY = "REFUND_ONLY"
    RETURN_REFUND = "RETURN_REFUND"

class AfterSaleClaimStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

class RefundStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
```

These are **claim** statuses. They are deliberately *not* the order's
`after_sale_status` axis (NONE/PROCESSING/PARTIAL_REFUNDED/REFUNDED), which is
the order's rollup. A claim reaching COMPLETED and the order reaching
PARTIAL_REFUNDED/REFUNDED are two different writes performed by the refund path.

`RefundStatus` matches the frontend's frozen `RefundRecord.status` union.

### 4.4 The three status axes Phase 5 writes (never derived from each other)

| Axis | Written by | Values used in Phase 5 |
|---|---|---|
| `orders.payment_status` | payment workflow + refund workflow | PAYING, PAID, PARTIAL_REFUNDED, REFUNDED |
| `orders.fulfillment_status` | fulfillment workflow | UNFULFILLED, PARTIAL_SHIPPED, SHIPPED, DELIVERED |
| `orders.after_sale_status` | **refund workflow only** | PROCESSING, PARTIAL_REFUNDED, REFUNDED |

**Shipping never changes `order_status`** (REQ-ORD-005, ORDER_WORKFLOW section 2).
**A claim never changes `after_sale_status`.** The claim has its own status
column; only money movement rolls the order up. If a rule you are about to write
makes one of these axes move as a side effect of another, stop.

## 5. Tables

All tables carry `PkMixin` + `TimestampMixin` + `MerchantScopedMixin` where a
merchant is derivable, and every money column is `MoneyMinor` (signed BIGINT - the
MySQL signed/unsigned CHECK trap in `HANDOFF.md` section 6 applies to every
constraint you write with arithmetic in it). Every status column gets a
hand-written `CHECK (col IN (...))` derived from the Python enum through the
`_sql_vocabulary` helper pattern used by `app/modules/order/models.py` - Alembic
does not autogenerate CHECK changes on MySQL, so these must be in the initial
`op.create_table` and any later change must be hand-written.

### 5.1 `payments` (REQ-PAY-001)

| column | type | notes |
|---|---|---|
| `payment_no` | varchar(32) | `UNIQUE (merchant_id, payment_no)`; `NVPAY<YYYYMMDD><id:06d>` |
| `order_id` | bigint unsigned FK orders.id | RESTRICT |
| `order_no` | varchar(32) | denormalised for list rendering (no join on a hot read) |
| `user_id` | bigint unsigned FK users.id | RESTRICT - the payer; ownership is applied in the query, never post-load |
| `merchant_id` | bigint unsigned FK merchants.id | from the order, never from input |
| `channel` | varchar(16) | CHECK IN channels |
| `amount` | bigint (MoneyMinor) | must equal `orders.payable_amount`; CHECK `amount > 0` |
| `status` | varchar(20) | CHECK IN record statuses |
| `external_transaction_no` | varchar(128) NULL | the provider's id, written only by a verified callback |
| `idempotency_key` | varchar(128) | `UNIQUE (merchant_id, idempotency_key)` |
| `client_request_id` | varchar(64) | `UNIQUE (user_id, client_request_id)` - the second guard (order table precedent) |
| `request_hash` | varchar(64) | sha256 of the canonical business inputs |
| `paid_amount` | bigint | what the provider actually settled; 0 until SUCCESS |
| `refunded_amount` | bigint | cumulative, written by the refund workflow |
| `expires_at` | datetime(3) NULL | the provider's payment window |
| `paid_at` | datetime(3) NULL | |
| `closed_at` | datetime(3) NULL | |

Constraints: `paid_amount >= 0`, `refunded_amount >= 0`,
`refunded_amount <= paid_amount` (this one is load-bearing for FG-12 - see
section 8), `amount >= 0`.

### 5.2 `payment_callbacks` (REQ-PAY-002, REQ-PAY-003)

| column | type | notes |
|---|---|---|
| `provider` | varchar(32) | e.g. `MOCK`, `ALIPAY` |
| `provider_event_id` | varchar(128) | the provider's own event id |
| `payment_no` | varchar(32) NULL | resolved from the payload; NULL when unknown |
| `order_no` | varchar(32) NULL | denormalised for triage |
| `payload_snapshot` | JSON | **sensitive-field filtered** - see 5.3 |
| `signature_valid` | bool | computed before insert; an invalid signature never reaches the workflow |
| `process_status` | varchar(16) | CHECK IN process statuses |
| `process_error` | varchar(500) NULL | business reason when FAILED |
| `event_type` | varchar(32) | `PAYMENT_SUCCEEDED` etc. |
| `received_at` | datetime(3) | |
| `processed_at` | datetime(3) NULL | |
| `attempt_count` | int | starts at 1 |

`UNIQUE (provider, provider_event_id)` is the whole point of the table. It is a
*global* unique (not merchant-scoped): an event id is unique at the provider.

### 5.3 Sensitive-field filtering (REQ-PAY-003) - a test must prove it

The snapshot is stored for audit and dispute handling, so it must be recognisably
the provider's payload - but a payment payload routinely contains secrets and
cardholder data. `providers.filter_callback_payload(payload)` returns a **new**
dict with any key whose lowercase name matches
`{password, passwd, secret, token, authorization, auth, api_key, apikey, sign,
signature, private_key, card_no, cardno, pan, cvv, cvv2, cvc, id_card,
bank_account, phone, email, key}`
replaced by `"***REDACTED***"`, recursively, at every depth, in both dicts and
lists of dicts. Unknown keys are kept. A test must feed a payload containing each
of those and assert none of the values survive, and must also assert a benign
field (e.g. `amount`, `trade_no`) is kept - a filter that redacts everything
passes a naive test while destroying the audit value.

### 5.4 `fulfillments` / `fulfillment_items` (REQ-FUL-001, REQ-FUL-002)

`fulfillments`: `fulfillment_no` (`UNIQUE (merchant_id, fulfillment_no)`,
`NVF<YYYYMMDD><id:06d>`), `order_id` FK RESTRICT, `order_no`, `merchant_id`,
`warehouse_id` FK RESTRICT (the deduction target), `fulfillment_status` CHECK,
`carrier` varchar(16) NULL, `tracking_no` varchar(64) NULL, `shipped_at`,
`delivered_at`, `package_count` int default 1, `remark` varchar(500) NULL.

`fulfillment_items`: `fulfillment_id` FK RESTRICT, `order_item_id` FK RESTRICT,
`sku_id` FK RESTRICT to `product_skus` (NOT NULL), `product_name` varchar(200),
`sku_name` varchar(200), `quantity` bigint CHECK `quantity > 0`.
`UNIQUE (fulfillment_id, order_item_id)` so one package cannot list the same line
twice (a duplicate would let a single package over-ship a line while each row looks
valid).

### 5.4a The `sku_id` column: what happened, and why it is here

This item took **five exchanges**, three reversals from the captain, and two members
implementing contradicted instructions. It is recorded at this length because the
reasoning is more useful than the outcome, and because the next reader will otherwise
find a comment somewhere saying the opposite.

**The column exists.** It is NOT NULL, FK'd to `product_skus`, indexed, supplied by
both insert sites (`FulfillmentService.create_shell` and `_create_residual_package`),
and backfilled from `order_items` for pre-existing rows during the migration.

**The wrong argument, held for most of the phase:** that `REQ-FUL-002`
(`fulfillment_items: fulfillment_id, order_item_id, quantity`) *forbids* `sku_id`.
It does not. It enumerates **required** columns, not an exhaustive set - the table
also carries `id`, `created_at`, `updated_at`, `product_name` and `sku_name`, and
**none of those five appear in that requirement either**. Nobody objected to them,
correctly, because the requirement says what must exist. The captain withdrew the
column ruling on this misreading, then argued the misreading back at the data-layer
for four rounds. **A requirement list is not a prohibition, and "it is not in the
list" is not a finding.**

**The argument that does have force, and the answer to it:** a stored `sku_id` is a
duplicate of a fact reachable through `order_item_id`, so it is a second place for a
shipping error to disagree with itself. That is true - and it applies identically to
`product_name` and `sku_name`, which this table already copies, for the two reasons
this table copies anything: a historical shipment must not be rewritten by a later
product edit, and the console's fulfillment queue must not need a join to render a
page. `sku_id` is the third field of the same snapshot, and the FK to `product_skus`
is what stops it disagreeing.

**Operational note from the migration**, contributed by the data-layer and better than
what was specified: a plain `ADD COLUMN ... NOT NULL` **fails** on this server when the
table already holds rows (`@@sql_mode` includes `STRICT_TRANS_TABLES`). The working
sequence is declare-nullable, backfill from `order_items`, then `ALTER ... MODIFY NOT
NULL`. An empty-table-only migration would have passed review and failed on any real
database.

**And the process lesson, which is the captain's:** three reversals came from sending
instructions composed from stale context. `API_CONTRACT.md` section 15.8 and the
protocol in section 13.2a exist to stop exactly that, and they were written for
everyone else. Before sending an instruction that changes a decision, re-read the
commit that made it.

Business rule: the **sum of shipped quantities per `order_item_id` across all
fulfillments of an order must never exceed that line's `quantity`**
(`FULFILLMENT_QUANTITY_EXCEEDS_ORDER`, 70001). Cumulative across packages - a
per-package check is not enough, and that is the defect this rule exists for.

### 5.5 `after_sales` / `after_sale_items` / `refunds` (REQ-AFS-001)

`after_sales`: `after_sale_no` (`UNIQUE (merchant_id, after_sale_no)`,
`NVAS<YYYYMMDD><id:06d>`), `order_id`, `order_no`, `user_id`, `merchant_id`,
`type` CHECK, `claim_status` CHECK, `requested_amount`, `approved_amount`,
`refunded_amount` (all MoneyMinor, all `>= 0`,
`refunded_amount <= approved_amount <= requested_amount` is **NOT** a constraint -
an operator may approve more than requested only if policy allows it, which V1
forbids in the service instead), `reason` varchar(500), `description`
varchar(1000) NULL, `evidence_urls` JSON NULL, `reject_reason` varchar(500) NULL,
`processed_at`, `completed_at`, `idempotency_key` UNIQUE per merchant,
`client_request_id` UNIQUE per user, `request_hash`.

`after_sale_items`: `after_sale_id`, `order_item_id`, `quantity` CHECK `> 0`,
`UNIQUE (after_sale_id, order_item_id)`.

`refunds`: `refund_no` (`UNIQUE (merchant_id, refund_no)`,
`NVR<YYYYMMDD><id:06d>`), `after_sale_id` FK RESTRICT, `order_id`, `order_no`,
`payment_id` FK RESTRICT NULL, `user_id`, `merchant_id`, `amount` MoneyMinor
CHECK `amount > 0`, `status` CHECK, `reason` varchar(500) NULL,
`operator_type` / `operator_id` (who executed it), `idempotency_key` UNIQUE,
`request_hash`, `completed_at`.

## 6. Workflows (order of operations is load-bearing)

### 6.1 `PaymentSuccessWorkflow` - one transaction, one commit

Trigger: a **verified** provider callback. Steps, in this order:

1. **Callback claim.** `PaymentCallbackRepository.insert_received(...)` attempts
   the insert. On duplicate key: load the existing row and return
   `CallbackOutcome(replayed=True, ...)` - no business effect. This is the FG-11
   serialisation point; it happens *first* so a duplicate can never reach step 3.
2. **Verify.** Upsert the callback row's `payload_snapshot` (filtered),
   `signature_valid`, and resolve `payment_no`. An unknown `payment_no` marks the
   row FAILED/IGNORED and returns without touching a payment.
3. **Load payment `FOR UPDATE`** by `payment_no`. If already `SUCCESS`, mark the
   callback PROCESSED (a concurrent duplicate that lost the insert race) and
   return. Non-SUCCESS statuses that are not `INITIATED`/`PAYING` are refused with
   `PAYMENT_STATE_INVALID` (60007).
4. **Amount guard.** Callback `amount` must equal `payments.amount`; mismatch is
   `PAYMENT_AMOUNT_MISMATCH` (60002) and the payment is NOT marked success.
5. **Load the order `FOR UPDATE`.** `order_status` must be
   `PENDING_PAYMENT`; if it is already `PROCESSING` (the duplicate-delivery
   case) treat as replay; if `CANCELLED`/`CLOSED`/`COMPLETED` refuse with
   `PAYMENT_ALREADY_PAID`/`ORDER_STATE_INVALID`.
6. **payment** -> `SUCCESS`, `paid_amount = amount`,
   `external_transaction_no`, `paid_at`. **order** `payment_status` -> `PAID`,
   `paid_amount = payable_amount`, `paid_at`; **order_status**
   `PENDING_PAYMENT -> PROCESSING` plus exactly one `order_status_logs` row
   (`operator_type = SYSTEM`).
7. **`ORDER_DEDUCT` inventory movement per line**, idempotency key
   `order-deduct:{order_no}:{order_item_id}` via
   `InventoryService.deduct(...)` with `reference_type=ORDER_ITEM`,
   `reference_id=order_item.id`. The key is what makes a *retried* workflow (after
   a crash between step 7 and the commit) unable to deduct twice.
8. **Fulfillment shell** - one `fulfillments` row in `UNFULFILLED` carrying one
   `fulfillment_items` row per order line (the full quantity). This is the
   "goods exist to be shipped" record; the customer may receive it in several
   packages later, and `POST /fulfillments/{id}/ship` decides the split.
9. **Order fulfillment_status** -> `PARTIAL_SHIPPED`? **NO.** Creating a package
   is not shipping it. Leave `UNFULFILLED`; the ship endpoint moves the axis.
10. **Outbox seam.** Phase 6 owns the table. Emit the same marked comment block
    that `CreateOrderWorkflow` step 9 carries, in this method, in this
    transaction - with an explicit line saying a duplicate callback must not
    produce a second outbox row (REQ-PAY-004's fourth clause).
11. **Mark the callback PROCESSED** (`processed_at`, `process_status=PROCESSED`),
    then commit once.

### 6.2 `RefundWorkflow` - the money fact (FG-12)

Trigger: `POST /after-sales/admin/after-sales/{after_sale_no}/refund`.

1. Claim the `Idempotency-Key` through `IdempotencyRepository` with scope
   `refund:execute` (same attempt-insert pattern).
2. Lock the after-sale row `FOR UPDATE`; it must be `APPROVED` or
   `PARTIAL_REFUNDED` (`AFTER_SALE_STATE_INVALID` otherwise).
3. Lock the payment row `FOR UPDATE` (the money source of truth).
4. **Revalidate both caps inside the lock, against freshly read rows:**
   * `refund.amount > 0` else `REFUND_AMOUNT_INVALID` (80006);
   * `payment.refunded_amount + amount > payment.paid_amount` ->
     `REFUND_EXCEEDS_PAID_AMOUNT` (80004);
   * `after_sale.refunded_amount + amount > after_sale.approved_amount` ->
     `REFUND_AMOUNT_INVALID`;
   * per line, when the claim carries items:
     `sum(previous refunded for that order_item) + this refund's share >
      order_item.payable_amount` -> `REFUND_EXCEEDS_ITEM_AMOUNT` (80005).
     The refund row carries no per-line split in V1, so the item cap is checked
     against the claim's item quantities: the refund's per-item share is
     `floor(amount * item_order_item_payable / approved_amount)` with the
     remainder on the last item - the same pro-rata-with-remainder rule
     `pricing/allocation.py` uses for INV-006, because two allocation algorithms
     that disagree produce an invariant that fails only on odd amounts.
5. Write: `refunds` row (`PENDING` -> `SUCCEEDED` in the same transaction; V1 has
   no asynchronous refund, so a PENDING row that nobody completes would be a lie),
   `payments.refunded_amount += amount`, `after_sales.refunded_amount += amount`,
   `after_sale_items`-side line refunds, `orders.refunded_amount += amount`,
   `orders.payment_status` -> `PARTIAL_REFUNDED` or `REFUNDED`
   (`REFUNDED` iff `refunded_amount == paid_amount`), `orders.after_sale_status`
   -> `PARTIAL_REFUNDED`/`REFUNDED` by the same rule, `after_sales.claim_status`
   -> `COMPLETED` when its own refunded total reaches `approved_amount`.
6. Inventory: a `RETURN_IN` movement per returned line **only for
   `RETURN_REFUND`** claims (goods came back), idempotency key
   `return-in:{after_sale_no}:{order_item_id}`. A `REFUND_ONLY` claim must not
   move stock - the customer kept the goods, and crediting them would inflate
   availability until the next stock count found it.
7. Outbox seam comment as in 6.1. Commit once.

**STRUCK during the build - the refund must NOT write `orders.fulfillment_status`.**
An earlier version of this step said a `RETURN_REFUND` whose lines have all come back
sets the axis to `DELIVERED`, reasoning that "a returned parcel *was* delivered". The
reasoning is backwards: a parcel that came back was delivered **earlier**, by a delivery
fact, and that fact is what should have set `DELIVERED` - not the return. Making the
refund the writer would mean a refund *creates* a delivery, which is precisely the
axis-collapsing section 31 forbids: it would let money movement assert a logistics fact.

So `RefundWorkflow` writes `orders.refunded_amount`, `payment_status` and
`after_sale_status`, and **nothing else**. `DELIVERED` arrives from a delivery event,
which Phase 5 does not have; when one lands it is the only writer of that value.
Raised by the after-sales author from a grep against the design, and recorded here
rather than in a handoff so the next reader does not implement the sentence.

### 6.3 `ShipWorkflow` (fulfillment)

`POST /fulfillments/{id}/ship` body is **exactly**
`{carrier, tracking_no, item_quantities: [{order_item_id, quantity}]}`:

1. Lock the fulfillment `FOR UPDATE`; a `SHIPPED`/`DELIVERED` row is
   `FULFILLMENT_ALREADY_SHIPPED` (70003).
2. Validate the requested lines are on this fulfillment
   (`FULFILLMENT_QUANTITY_EXCEEDS_ORDER` when a requested quantity exceeds the
   package line) and that the request is non-empty.
3. Stamp `carrier`, `tracking_no`, `shipped_at`, and the shipped quantities on
   `fulfillment_items`.
4. Recompute the order's `fulfillment_status` from **all** its packages: every
   line's total shipped quantity `>= quantity` -> `SHIPPED`; some shipped ->
   `PARTIAL_SHIPPED`; nothing -> `UNFULFILLED`. Never touch `order_status`.
5. If the request ships less than a package line, the remainder stays shippable as
   a **new** package (multi-package per order, REQ-FUL-001): create the residual
   fulfillment row in the same transaction rather than dropping it.

## 7. HTTP surface (the captain freezes this in `API_CONTRACT.md` section 15)

Frozen task endpoints already in the baseline: `POST /api/v1/fulfillments/{id}/ship`.
Discovery endpoints already frozen by `API_CONTRACT.md` section 5:
`GET /api/v1/fulfillments/admin`, and `shipments[]` inside `GET /orders/{order_no}`.

The rest is Phase 5's, matching the paths the frontend already calls
(`frontend/src/api/endpoints.ts`) - **the backend conforms to the frozen frontend
paths, not the other way round**:

| Method + path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/payments/customer/payments` | JWT (owner) | create a payment attempt; `Idempotency-Key` required |
| `GET /api/v1/payments/customer/payments/{payment_id}` | JWT (owner) | one payment |
| `GET /api/v1/payments/customer/payments/by-order/{order_no}` | JWT (owner) | the order's payment |
| `POST /api/v1/payments/customer/payments/{payment_id}/mock-pay` | JWT (owner) + DEV/DEMO | drives the *same* `PaymentSuccessWorkflow` through a mock provider event; refused unless `PAYMENT_MOCK_ENABLED` and `APP_ENV in {dev,test,demo}` |
| `POST /api/v1/payments/callbacks/mock/{payment_id}` | **provider auth** (HMAC signature), no JWT | the real callback surface used by the mock provider and by FG-11 |
| `GET /api/v1/payments/admin/payments` | console | paged list, filters `status`, `order_no`, `channel` |
| `POST /api/v1/after-sales/customer/after-sales` | JWT (owner) | apply; `Idempotency-Key` required |
| `GET /api/v1/after-sales/customer/after-sales` | JWT (owner) | paged list |
| `GET /api/v1/after-sales/customer/after-sales/{after_sale_no}` | JWT (owner) | detail |
| `POST /api/v1/after-sales/customer/after-sales/{after_sale_no}/cancel` | JWT (owner) | cancel a PENDING claim |
| `GET /api/v1/after-sales/admin/after-sales` | console | paged queue |
| `GET /api/v1/after-sales/admin/after-sales/{after_sale_no}` | console | detail |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/approve` | console | approve (amount may be lower) |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/reject` | console | reject + reason |
| `POST /api/v1/after-sales/admin/after-sales/{after_sale_no}/refund` | console | execute `RefundWorkflow`; `Idempotency-Key` required |
| `GET /api/v1/fulfillments/customer/orders/{order_no}/shipments` | JWT (owner) | the consumer's view of its packages |
| `GET /api/v1/fulfillments/admin` | console | fulfillment queue (section 5.2) |
| `POST /api/v1/fulfillments/{id}/ship` | console | frozen task endpoint |

Provider authentication model (REQ-PAY-005): callbacks carry headers
`X-Provider`, `X-Provider-Event-Id`, `X-Provider-Timestamp`, `X-Provider-Signature`.
The signature is `HMAC-SHA256(secret, f"{timestamp}.{raw_body}")` in hex, where
the secret is `PAYMENT_CALLBACK_SECRET` (per provider in
`PAYMENT_PROVIDER_SECRETS` for non-mock providers). A bad signature is
`PAYMENT_CALLBACK_INVALID_SIGNATURE` (60003) with **no** callback row that claims
success. A stale timestamp (> `PAYMENT_CALLBACK_MAX_SKEW_SECONDS`) is refused
too - a replay of a captured body is the attack this exists for.

## 8. The FG-12 caps, at the database boundary

Application checks are not the boundary. Three of them, one per cap, all

* `payments`: `CHECK (refunded_amount <= paid_amount)`
* `orders`: `CHECK (refunded_amount <= paid_amount)`
* `order_items`: `CHECK (refunded_amount <= payable_amount)`

plus the per-line cap is enforced in `RefundWorkflow` because it is a *sum over
refunds per line*, which a single-row CHECK cannot express. FG-12 must prove,
with a negative control, that a direct `UPDATE`/`INSERT` through a **fresh
connection** that violates each cap is refused by MySQL (errno 3819 for a CHECK),
not merely by Python. `HANDOFF.md` section 6's REPEATABLE-READ warning applies:
read the post-mutation state on a fresh connection or the assertion compares the
snapshot against itself and passes as a false green.

## 9. Error codes (already defined in `app/core/errors.py` - use these, do not invent)

| code | number | when |
|---|---|---|
| `PAYMENT_NOT_FOUND` | 60000 | foreign/missing payment - never 403 (section 109) |
| `PAYMENT_ALREADY_PAID` | 60001 | callback for an order that is not payable |
| `PAYMENT_AMOUNT_MISMATCH` | 60002 | callback amount != payment amount |
| `PAYMENT_CALLBACK_INVALID_SIGNATURE` | 60003 | bad/missing/stale signature |
| `PAYMENT_CALLBACK_DUPLICATE` | 60004 | duplicate event - **HTTP 200**, an idempotent success, not an error |
| `PAYMENT_CHANNEL_UNSUPPORTED` | 60005 | channel not in the enabled set |
| `PAYMENT_MOCK_DISABLED` | 60006 | mock surface used outside dev/test/demo |
| `PAYMENT_STATE_INVALID` | 60007 | payment record not in a state that can be settled |
| `FULFILLMENT_NOT_FOUND` | 70000 | |
| `FULFILLMENT_QUANTITY_EXCEEDS_ORDER` | 70001 | cumulative shipped > ordered |
| `FULFILLMENT_STATE_INVALID` | 70002 | not shippable |
| `FULFILLMENT_ALREADY_SHIPPED` | 70003 | |
| `AFTER_SALE_NOT_FOUND` | 80000 | |
| `AFTER_SALE_NOT_ELIGIBLE` | 80001 | order/lines not eligible (unpaid, cancelled, nothing left to claim) |
| `AFTER_SALE_STATE_INVALID` | 80002 | |
| `REFUND_NOT_FOUND` | 80003 | |
| `REFUND_EXCEEDS_PAID_AMOUNT` | 80004 | cap 1 |
| `REFUND_EXCEEDS_ITEM_AMOUNT` | 80005 | cap 2 |
| `REFUND_AMOUNT_INVALID` | 80006 | <= 0, or above the approved amount |
| `REFUND_ALREADY_COMPLETED` | 80007 | refund idempotency-key reuse with a different body |

Every one of these already has a canonical HTTP status in
`app/core/errors.py::_DEFAULT_HTTP_STATUS` - read it, do not hand-pick a status.

## 10. Settings added by Phase 5 (captain owns `app/core/config.py` + `.env.example`)

```python
PAYMENT_MOCK_ENABLED: bool = True            # forced False outside dev/demo by validation
PAYMENT_CALLBACK_SECRET: SecretStr = SecretStr("dev-mock-callback-secret")
PAYMENT_CALLBACK_MAX_SKEW_SECONDS: int = Field(default=300, ge=30)
PAYMENT_ENABLED_CHANNELS: list[str] = ["MOCK", "ALIPAY", "WECHAT"]
PAYMENT_NO_PREFIX: str = "NVPAY"
FULFILLMENT_NO_PREFIX: str = "NVF"
AFTER_SALE_NO_PREFIX: str = "NVAS"
REFUND_NO_PREFIX: str = "NVR"
REFUND_MAX_AMOUNT_RATIO: ... # not needed - no ratio cap exists in the baseline
```

`PAYMENT_MOCK_ENABLED` must be **validated against `APP_ENV`**: a model validator
that raises when `APP_ENV in {staging,prod}` and the flag is true. That is the
setting-level half of REQ-PAY-005; the endpoint-level half asks the same question
again, because a validator someone bypasses with `model_construct` would
otherwise be the only guard (INV-008).

## 11. Test plan / what "done" means

Every implementer: `pytest` green for your own module **and** `ruff check` clean
for every file you touched, and `alembic check` reporting no new operations.
Nothing is claimed until it has been run. Per HANDOFF section 17.6 the order is
implement -> static check -> migration -> test -> run -> fix -> evidence -> docs.

Gate owners:

* **FG-11** (`artifacts/evidence/concurrency/fg11_payment_idempotency.json`) -
  captain, on a test the *payment* author writes
  (`backend/tests/concurrency/test_payment_idempotency.py`): real MySQL, real
  threads, the **same provider event delivered N times** (N >= 10) concurrently,
  asserting exactly one payment SUCCESS, one `ORDER_DEDUCT` movement per line,
  one fulfillment + its items, one `PENDING_PAYMENT -> PROCESSING` status log, one
  callback row in PROCESSED, and every loser answering the duplicate code. Plus a
  **negative control**: a second, *distinct* event id for the same payment must be
  refused by the state guard (proving the guard, not the unique index, is what
  stops double settlement).
* **FG-12** (`artifacts/evidence/integration/fg12_refund_invariants.json`) -
  verifier writes `backend/tests/integration/refund/test_refund_invariants.py`;
  the captain emits the artifact. Must include the three database-boundary
  negative controls of section 8, on fresh connections, plus the per-line
  cumulative cap through `RefundWorkflow`, plus a positive control that a legal
  partial refund followed by a legal second refund reaches exactly
  `refunded == paid` and a third is refused.

## 12. File-ownership map (the collaboration hazard, HANDOFF section 15.3)

**One file, one writer.** Never `git add -A`; commit path-scoped. If you need a
change in a file you do not own, message the owner (or the captain) - do not edit
it, and do not "just fix it quickly" because it is small.

| Path | Owner |
|---|---|

> **Corrected during the build.** This table first assigned
> `{payment,fulfillment,aftersales}/{models.py,enums.py}` to data-layer as one row, which
> contradicted section 4.3: that section writes the after-sales vocabularies as part of
> the frozen contract, i.e. the after-sales author's. The tree is coherent -
> `aftersales/models.py` imports the vocabularies that `aftersales/enums.py` declares -
> but the stale row was a real hazard: the next reader who "restored" the assignment
> would have overwritten a live file, or added a second copy of the vocabulary, and a
> duplicated vocabulary is how the Python enums and the `CHECK` constraints derived from
> them diverge silently (the Alembic-cannot-see-CHECK-changes trap, HANDOFF section 6).
> `payment/enums.py` and `fulfillment/enums.py` genuinely are data-layer's.| `backend/app/modules/payment/**`, `backend/tests/**/payment/**`, `backend/tests/concurrency/test_payment_idempotency.py` | payment-workflow |
| `backend/app/modules/fulfillment/**`, `backend/tests/**/fulfillment/**` | fulfillment-workflow |
| `backend/app/modules/aftersales/**`, `backend/tests/**/aftersales/**` | after-sales-workflow |
| `backend/app/modules/{payment,fulfillment}/enums.py` | data-layer |
| `backend/app/modules/aftersales/enums.py` | after-sales-workflow |
| `backend/app/modules/{payment,fulfillment,aftersales}/models.py`, `backend/migrations/versions/**` | data-layer |
| `backend/app/modules/order/serializers.py`, `backend/app/modules/order/repository.py`, `backend/app/modules/order/service.py`, `backend/app/modules/order/workflow.py` | captain |
| `backend/app/api/v1/router.py`, `backend/app/core/config.py`, `.env.example`, `docs/architecture/**`, `scripts/**`, `HANDOFF.md`, `PROJECT_BASELINE.yaml` | captain |
| `backend/tests/integration/refund/**` | verifier |
| `backend/tests/conftest.py` (the broken shared `client` fixture) | captain |
| `frontend/**` | captain (another agent session may be editing it - HANDOFF 15.3) |

Shared read-only helpers live in `backend/tests/integration/commerce/seed.py`
(owned by data-layer): every Phase 5 test imports its fixtures/helpers from
there rather than re-deriving a merchant/SKU/warehouse seed four times.

## 13. Measurement protocol and defect reporting (added mid-phase, after four false alarms)

Four writers share one working tree and **one MySQL database**. That is the phase's
biggest source of wrong information, and it produced four wasted rounds in one
afternoon. The rules below are not ceremony; each one exists because something
specific went wrong.

### 13.1 One test process at a time

Two `pytest` processes against the same MySQL will interleave DDL and DML, and the
loser reports **errors that do not exist**. Observed: a full-suite run showed four
`ERROR`s in the order integration tests; each of those tests passed in isolation
seconds later. A concurrent pair of runs also turned a 3.5-second FG-11 gate into a
60-second run with 3 deadlock failures, which looked like a flaky gate and was not.

So: before running `pytest tests`, say so in the team channel and wait for the
captain's go-ahead. Fast single-file runs are fine; whole-suite runs are queued.

### 13.2 A defect report must name the commit it was observed at

Adopt this unconditionally. Three of the four false alarms were reports of a defect
that had **already been fixed** by the time anyone read the report:

* a `NameError` in `payment/models.py` was reported by three different people across
  three turns, after it was fixed;
* an `AppError` `NameError` in `fulfillment/service.py` was reported after its fix
  had landed (it had been real for one intermediate commit);
* a failure in `test_payment_idempotency.py` was reported after it had been fixed.

Every one cost a turn to disprove, and the disproof always had the same shape:
"this passes on HEAD". Include `git rev-parse --short HEAD` in the report. If the
report says an older commit, the first response is to re-run at HEAD.

### 13.2a The prophylaxis, because five reports were filed against one stale snapshot

Five separate reports were filed against the same pre-`a785744` state - two of them
about a `NameError` that had been removed before that commit, one claiming an
`ImportError` for a file that had been tracked for hours, one claiming `alembic`
failed outright while it was clean, and one claiming tests were skipped for missing
tables while all seven existed and `0 skipped`.

Worse than the wasted turns: **the reports began contradicting each other**, which
means that in that window nobody could tell a real defect from an observer's stale
cache - and FG-11/FG-12 readings taken in such a window could pass or fail on the
observer's state rather than on the code.

So before filing a defect, run these three and paste the output into the report:

    git rev-parse --short HEAD
    git status --short <the path you are reporting on>
    ..\.venv\Scripts\python.exe -m alembic current

A stale `.pyc` and a stale editor buffer are the two plausible causes. If the file on
disk disagrees with `git ls-files` in the same repo, that is a working-tree divergence
and is worth escalating loudly rather than explaining away as a cache.

### 13.3 Re-run at HEAD before escalating

Before telling another owner their file is broken: re-run your repro at HEAD. If it
still fails, report it. If it does not, say so and drop it - a stale defect report
costs the owner a turn and costs the reporter credibility.

### 13.4 Evidence is emitted from a CLEAN tree only

`scripts/gate_evidence.py` records the revision and **refuses to emit a PASS when the
tested paths are dirty relative to it** (the artifact still records the run and marks
it FAIL with the reason, because a suppressed run is worse than a failed one). This
was added after FG-11 was emitted once from a tree that was later found to be flaky:
the artifact said `PASS` and gave no way to tell which revision it described, so it
could not be retired, only suspected.

Consequence: the captain calls a **freeze** - nobody writes - commits, checks the tree
is clean, and *then* emits. An evidence artifact is a claim about a revision.

### 13.5 The database is shared state, and a failed test skips its teardown

This one cost two people real time, and it is the most misleading of the five.

The integration fixtures **commit** their seeds (they must: a payment callback arrives
on another connection, and a rolled-back fixture would be invisible to it) and delete
them in teardown. A test that *fails* never runs its teardown, so every failure leaves
its merchant, products, warehouse, orders, payments and fulfillments behind.

Left-over rows then change the behaviour of later tests, because the seeds resolve
things like a **default warehouse** by querying "the active one for this merchant" -
and an orphaned row from an earlier run can shadow the one the current test just made.
The observed symptom was brutal: shipping worked, but `fulfillment_status` stayed
`UNFULFILLED`, producing six failures that looked exactly like a real regression and
vanished on a clean database. At the worst point the shared `nova` schema held ~20
orders, 24 warehouses, 41 users and 76 payment callbacks of residue.

Consequences, both mandatory:

* **A test count is only meaningful together with the state of the database it was
  measured on.** Before reading anything into a number, establish that the schema holds
  no residue from earlier failed runs.
* **A red run must be re-confirmed in isolation before it is believed**, because the
  second-order failures it creates are indistinguishable from real ones.

### 13.6 DDL has no transaction semantics - never inside a savepoint you intend to roll back

A verifier proved the three caps are enforced by the **live** tables (not only by
synthetic twins) and, to complete the argument, added a *meta-control*: drop the
constraint, re-issue the same violating write, show it now lands - which is what
proves the rejection came from the cap rather than from something incidental.

The probe wrapped that in a savepoint and rolled back. **MySQL performs an implicit
commit on DDL.** `ALTER TABLE ... DROP CHECK` closed the transaction and destroyed
the savepoint, so the rollback undid nothing: the constraint was gone from the shared
schema and the probe row was committed. It was caught by a whole-table re-read (the
`refund_cap` count came back 2, not 3), repaired from the identical clause on another
table rather than by retyping it, and verified three ways - it enforces again, it is
byte-identical to what the migration produces (a `downgrade`/`upgrade` round trip),
and `alembic check` is clean. The round trip also emptied `payments`, which held
accumulated residue rather than fixtures.

Two rules:

* **A meta-control that mutates DDL belongs on a table the test owns outright** -
  `CREATE TABLE` a twin, attach the clause, `DROP CHECK` it explicitly, `DROP TABLE`
  in teardown. No transaction need be relied on and no shared object is at risk.
* **After any DDL probe, re-read the catalogue and assert the object count**, not the
  exit code of the repair. The damage here was invisible until the count was checked.

The same probe also produced the phase's most valuable negative-control technique:
against the **live** tables a violating write is refused with **errno 3819 naming the
real constraint**, with the row byte-identical when re-read on a **separate
connection**. It also showed why a live-table probe needs care that a twin does not -
one attempt was refused by a *unique key* (`1062`) rather than the cap, so the probe
must be built so the unique keys cannot be what refuses it, and the assertion must
name errno 3819 explicitly.

`scripts/verify_refund_caps.py` re-reads the three clauses from
`information_schema` and asserts the **expression**, not just the name - a constraint
re-created with the wrong clause would satisfy a name-only check.

### 13.6a Two measured corrections to the twin construction (13.6)

Both were measured by the verifier after 13.6 was written, and both change the recipe:

* **Build the twin from an explicit column list, never `CREATE TABLE ... LIKE`.** On
  MySQL 8.4 `LIKE` **does** copy CHECK constraints - it renames them to
  `<table>_chk_N`, which is why an earlier probe filtered `CHECK_CONSTRAINTS` by name,
  found nothing, and wrongly concluded the constraints were absent. It also copies
  keys (3 of them on `payments`). A `LIKE` twin is therefore neither constraint-free
  nor key-free, so a rejection on it has several possible causes and the control
  measures the wrong thing. **Reading a filtered view is not reading the schema** -
  the same error class as the rest of this phase.
* **Attach the clause BEFORE inserting the violating row.** MySQL validates a new
  `CHECK` against existing rows, so the other order fails on the `ALTER` itself with
  `(3819, "Check constraint '<name>' is violated.")`. Corollary worth knowing about
  your own schema: on a table that is legitimately mid-drift, a cap cannot be added at
  all until the data satisfies it.

Additions: `CHECK` names are unique per **schema**, not per table (`3822` on a
duplicate); and a savepoint row is **invisible to another connection** until the outer
transaction commits, so a cross-connection re-read taken before that commit returns
`None` and proves nothing.

### 13.6b The shared database has no isolation between concurrent test runs

This is the mechanism behind most of the day's wobbling numbers, and it was measured
rather than suspected. Four `pytest` processes were live against the same `nova`
schema at once, and all three observed failures were on rows a fixture had **just
created**:

* `StaleDataError: UPDATE statement on table 'users' expected to update 1 row(s); 0 matched`
* `AssertionError: the fixture's account is missing entirely`
* `ORDER_NOT_FOUND` from `RefundWorkflow` on an order `paid_order()` had created moments earlier

The evidence that it is environmental rather than a defect: the same suite with no
competitor gives **48 passed repeatedly**; an 8-run loop gave **8/8 green** while
another agent was quiet and **1-2 failures per run** while they were active; and the
failing **set moves** between runs (HTTP tests one run, workflow tests the next),
which is the signature of a shared mutable resource, not a deterministic bug.

`orders`/`order_items`' `RESTRICT` foreign keys make it worse rather than better: a
purge racing another run's purge hits `1451 Cannot delete a parent row`, and its delete
rolls back. The data is left consistent, but the teardown raises.

Two consequences, and the second is the one that matters for evidence:

* **A re-run during another agent's suite is a coin flip on somebody else's teardown.**
  Any number quoted while others are testing is unreliable, including the captain's.
* **An evidence emitter that runs during a teammate's test run can record pure
  interference as failure.** Gate artifacts must be emitted with the other process
  stopped - which is what the freeze window in 13.7 is for, and it is the reason the
  emitters record a residue line and a revision.

**A related measurement caveat, contributed by the data-layer author:** a
`residue: 0` reading is only meaningful *together with the concurrency state*, exactly
as a test count is only meaningful together with the database state. The residue tool
reports rows and orphans but says nothing about who else is running, so `residue: 0`
can be true mid-fixture while another process is mid-insert. Quote it with the process
count or not at all.

**And the two-way trap that follows from this whole section:** a flake whose failing
**set moves** between runs, and which disappears when run alone, is environmental
evidence - a deterministic bug does not behave that way. But once you have seen one such
flake, the mirror error is to dismiss a **stable** failure as environmental. The moving
set is the signal, not the mere fact that a failure vanished in isolation.

The durable fix is per-worker schema isolation (a distinct `MYSQL_DATABASE` per
process) or serialised runs. Neither was implemented in this phase; both are recorded
here as the next structural improvement, and until one lands the freeze window is the
only protection.

### 13.7 The freeze window

When the captain sends `FREEZE`, stop writing to the repository. Finish the tool call
you are in, report, and wait. The window exists to make section 13.4 possible, and it
is short.
