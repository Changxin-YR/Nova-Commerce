# Phase 5 contract contradictions (read-only sweep)

Captain-requested sweep: `PROJECT_BASELINE.yaml` REQ-* entries for the Phase 5 domains
against `PHASE5_DESIGN.md` and `API_CONTRACT.md`, reporting only mismatches a reader could
act on. **Read-only: no document and no code was changed.** Verified at HEAD `f719c2c`,
`alembic current` = `3f1ae2c55c54`, DB read via `information_schema`.

Context: four contradictions cost a round trip each this phase (`aftersales/enums.py`,
`payment/models.py`, carrier vocabulary, `sku_id`). They share one cause - the design
document and the baseline disagree in more than one place - so this is the mechanical
sweep for the rest.

## C1. `payment_no` uniqueness: baseline says GLOBAL, schema enforces PER MERCHANT  (ACTION)
- **Baseline** `REQ-PAY-001` (L460): *"payments: payment_no UNIQUE, order_id, channel,
  amount, status, external_transaction_no, idempotency_key"*.
- **Design** §5.1: *"UNIQUE (merchant_id, payment_no)"*.
- **Code (neither doc, the schema):** `uq_payments_merchant_payment_no -> (merchant_id, payment_no)`.

The baseline's `payment_no UNIQUE` is a plain column-level unique; the design and the
schema scope it by merchant. This is the **only real baseline-to-schema divergence I found
in the Phase 5 columns**, and it is a semantic difference, not formatting: under the scoped
key two merchants may hold the same `payment_no`; under the baseline's rule they may not.
**Keep:** the scoped key (`(merchant_id, payment_no)`), because V1's single merchant is a
deployment choice and `payment_no` embeds an auto-increment id that is only unique per
merchant in any case. **But the baseline is wrong as written and should say
`UNIQUE (merchant_id, payment_no)`** - otherwise a reader implementing from the baseline
will add a redundant global unique and break multi-merchant seeding. The identical scoping
question applies to `fulfillments.fulfillment_no`, `after_sales.after_sale_no`,
`refunds.refund_no` (all scoped in both design and code), but the baseline does not spell
those out, so only `payment_no` is a stated-vs-actual conflict.

## C2. `fulfillment_items.sku_id`: baseline says three columns, code has FOUR  (ACTION)
- **Baseline** `REQ-FUL-002` (L469): *"fulfillment_items: fulfillment_id, order_item_id, quantity"*.
- **Design** §5.4 currently reads (post-`a01271e`): *"there is no `sku_id` column, and this
  document originally said there was."*
- **Code:** the column **exists**, is `NOT NULL`, FK to `product_skus`, and is indexed
  (`a3a6dd1`, `0a4e136`, `b73a6f4`).

The captain's latest ruling is option (b) - derivation, no column - and says
fulfilment-workflow "will keep their batched join as the permanent answer". **That premise
is not what the tree contains:** fulfilment's committed code reads the column
(`serializers.py:75` `sku_id=row.sku_id`, docstring at :120 *"each item now carries its own
`sku_id` column"*) and writes it (`service.py:319,744`), and their commit `fbf265e` is titled
*"make the sku_id map required"*. `order/serializers.py` is the side that derives.
**Keep:** whichever the captain confirms - but removal is not a one-file change. If (b):
revert `a3a6dd1`/`0a4e136`/`b73a6f4` **and** fulfilment reverts `row.sku_id` + both write
sites in the same window, or their module raises `AttributeError` and their inserts fail
`NOT NULL`. My recommendation remains keeping the column: it is green, load-bearing in
another owner's committed code, and a snapshot row carrying the names *and* the id is
self-consistent. **This is C1 of my t7 status too - unresolved, schema held.**

## C3. Not contradictions, recorded so the sweep is auditable
Checked and **consistent** - listing them so "sweep clean" is a claim with scope, not a
silence:
- `REQ-PAY-002` callback columns: all six named columns exist; seven extras
  (`order_no`, `merchant_id`, `process_error`, `event_type`, `received_at`,
  `processed_at`, `attempt_count`) are design §5.2 additions, not conflicts.
- `payment_callbacks` unique is `(provider, provider_event_id)` - **global**, matching the
  design's explicit "an event id is unique at the provider". No baseline conflict.
- `REQ-AFS-001` (claims and refunds are separate tables) - matches: `after_sales` +
  `after_sale_items` + `refunds` exist as three tables.
- `REQ-AFS-002` (total refunded <= paid; item refund <= item payable) - enforced at the
  database boundary: `ck_payments_refund_cap`, `ck_orders_refund_cap`,
  `ck_order_items_refund_cap` all present.
- `REQ-ORD-006` (separate status vocabularies) - DB CHECK vocabularies match the Python
  enums for `payments.status`, `payments.channel`, `after_sales.claim_status`,
  `refunds.status`.

## C4. What this sweep did NOT cover (do not read it as complete)
- I checked the Phase 5 REQ entries by reading the baseline and querying
  `information_schema`; I did **not** exhaustively diff every prose sentence of
  `PHASE5_DESIGN.md` against `API_CONTRACT.md`. Contradictions that exist only between
  those two documents, with no column or constraint to query, would not surface this way.
- `API_CONTRACT.md` was consulted for the `sku_id` / Fulfillment shape only. Its other
  Phase 5 sections (15.x endpoint tables, error-code mapping) were not diffed against
  `app/core/errors.py` or the routers.
- No behavioural claims: a rule can be stated consistently in both documents and still be
  implemented wrongly. This sweep finds *documentary* contradictions only.
- Consequence: C1 and C2 are the actionable output; the absence of C5 does not mean the
  contract is now self-consistent.
