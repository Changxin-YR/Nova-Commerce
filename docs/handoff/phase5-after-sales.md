# Phase 5 handoff - after-sales (claims, `RefundWorkflow`, refund caps)

Scope: `aftersales` only. Written by after-sales-workflow on request from the captain, at
`41d4d0e`. Read `docs/architecture/PHASE5_DESIGN.md` section 4.3, section 5.5, section 6.2, section 7, section 8, section 9 first.

## 1. Files I own

- `enums.py` - `AfterSaleType`, `AfterSaleClaimStatus`, `RefundStatus`; carrier names **re-exported**
 from `fulfillment.enums` (no local list)
- `service.py` - `AfterSaleService`: apply / cancel / approve / reject, reads, queues; eligibility
 computed from persisted rows, never from the request
- `workflow.py` - `RefundWorkflow`: locks, both caps, money writes, `RETURN_IN`, outbox seam, one commit
- `refunds.py` - split + per-line cap arithmetic (`allocate_refund_across_lines`, `check_per_line_cap`)
- `schemas.py` / `serializers.py` / `views.py` - wire shapes matching `frontend/src/types/domain.ts`
- `api/{customer,admin,refunds}.py` - the section 7 endpoints; `RefundRequest` needs the key in header
 **and** body, mismatch refused
- `tests/unit/modules/aftersales/test_refund_split.py` - split/cap arithmetic, no DB
- `tests/integration/aftersales/{conftest,test_refund_workflow,test_http_endpoints}.py` - real MySQL
 and HTTP through the real app

**Not mine - do not edit:** `aftersales/models.py`, `aftersales/repository.py`,
`aftersales/__init__.py` (data-layer); `order/**` (captain); `tests/integration/refund/**`
(verifier); `tests/integration/commerce/seed.py` (data-layer).

## 2. Done and committed

**Everything of mine is committed - nothing is loose in the working tree.** At `41d4d0e`, and
`git status` is clean for all paths above.

* `745b90d` - final working-tree edit at freeze (service, workflow, refunds, schemas, serializers, views, api, tests)
* `13ad7b5` - test adjustments for the shared seed's new `shop(engine, request)` signature
* `41d4d0e` - `refunds.py`: replaced the dead store with a real, capacity-checked delegation to `pricing.allocation.allocate_pro_rata`

Measured at `41d4d0e`, my two directories only (whole suite not run - section 13.1):

```
pytest tests/unit/modules/aftersales tests/integration/aftersales   ->  49 passed
ruff check app tests migrations                                     ->  All checks passed!
alembic check                                                       ->  No new upgrade operations detected
```

## 3. Not done / priority-ordered

1. **Nothing blocking.** The four items the captain listed as open are all **already fixed** - do not spend a turn re-deriving them:
  (a) the `conftest.py` import error (`load_claim`, `available_stock`) was a mid-edit window; both are defined at `conftest.py:602`/`:664` and exported; `--collect-only` gives 49, 0 errors;
  (b) the cap tests assert **which** cap refused - see `test_cap_one_and_cap_two_are_distinguishable_by_code` (80004 with `paid_amount`/`refundable` and `line_remaining` asserted *absent*; 80005 with `line_remaining` and `paid_amount` asserted *absent*);
  (c) the `refunds.py` dead store is fixed at `41d4d0e`;
  (d) carrier vocabulary is re-exported from `fulfillment.enums` (verified by object identity).
2. **FG-12 / t6 - the verifier's gate.** Not mine; next command is the captain's emitter.
3. **Any change to the shared seed or the schema must be re-verified against my suite** - see trap 4.

## 4. Traps that cost real time

1. **`RETURN_IN` must use `InventoryService.return_in()`, not `adjust()`.** `adjust` is the operator optimistic-locking path: wrong signature (`reference_type` rejected) *and* wrong movement type (`MANUAL_ADJUST`/`MANUAL`), so a return would read as a human stock count in the ledger. Data-layer added `return_in()`; it is replay-safe on `idempotency_key`, does `available_qty += q` and leaves `locked_qty` alone. Mine is called only for `RETURN_REFUND`; `REFUND_ONLY` moves no stock.
2. **Cap 1 (80004) is not reachable through the workflow**, because cap 2 implies it: `sum(order_items.payable_amount) == orders.payable_amount == payments.paid_amount`, so any refund above `paid - refunded` also exceeds some claimed line's remaining capacity. Do not write an FG-12 case expecting 80004 from the workflow - it will fail for arithmetic, not a bug. Test the guard against the state it exists to refuse (both my probe claims are built by direct INSERT) and assert the *context shape*, which is what makes a wrong-cap refusal fail loudly.
3. **The allocation basis.** Weight each line by its **remaining** capacity (`payable_amount - refunded_amount`), not its total payable: `sum of shares == amount, and share_i <= line_i.remaining`. The design's literal `amount * payable / approved_amount` over-commits a line when `approved_amount < sum(claimed payables)`; the captain is correcting section 6.2.
4. **A concurrent downgrade can delete the Phase 5 tables mid-run.** I hit 30 phantom `1146 Table 'nova.payments' doesn't exist` errors with `alembic current` at Phase 4's revision; I restored it with `alembic upgrade head`. **Check `alembic current` is `3f1ae2c55c54` before trusting any result.** For the same reason, do not read a red run as a code fault while another pytest process is live (section 13.1) - my own runs went 49 passed -> 30 errors -> 49 passed on identical code.

## 5. Interfaces other modules depend on

`RefundWorkflow(session).execute(*, principal, after_sale_no, amount, idempotency_key, reason=None) -> RefundOutcome`
(`RefundOutcome.refund`, `.replayed`, `.split`). It takes no prices from the caller, and commits exactly once.

Repository/service method names my code actually calls (data-layer verified these by introspection; `service.add`, `add_item`, `approve`, `reject`, `cancel`, `apply_refund`, `get`, `get_by_after_sale_no`, `get_by_after_sale_no_for_update`, `get_by_idempotency_key`, `items_for`, `list_customer_claims`, `list_admin_claims`, `list_for_order`; `refunds.insert_succeeded`, `refunds.get_by_idempotency_key`, `refunds.list_for_after_sale`; `payments.get_latest_for_order`, `payments.get`; `orders.get_by_order_no_for_update`, `orders.get`, `orders.items_for`; `IdempotencyRepository.get`/`mark_completed`).

Note `get_by_after_sale_no_for_update(after_sale_no)` takes **no scope filter** - I do a scoped read first, then lock, so ownership stays in the query (a post-load check is one early return away from leaking existence).

## 6. What I could not verify

* **Cap 1 is not exercised end-to-end.** I could not construct a workflow path where 80004 is the guard that fires, for the arithmetic reason in trap 2. I tested it against a directly-constructed state instead. If a real over-refund guard is ever needed, this is the gap.
* **Concurrency.** No test of two simultaneous refunds on one claim/order, and none of refunds racing a shipment. The lock order (claim -> payment -> order -> lines sorted by id) is reasoned, not measured; the verifier should probe it.
* **`RETURN_IN` stock effects under retry across processes** - I assert the idempotency key and that a replay adds no movement, but only within one process.
* **The HTTP tests log in for real**, so they mutate global `users` state and are the most contention-sensitive of my files; under concurrent runs they can fail on rows a fixture just created.
* **`RefundWorkflow` does not write `orders.fulfillment_status`, and that is now the settled
  ruling rather than a gap.** Design 6.2 originally said the refund should set `DELIVERED` once a
  `RETURN_REFUND` claim had every line back; the captain struck that sentence at `e423e4f`, on the
  ground that a parcel returned *was* delivered earlier by a delivery fact, so making the refund the
  writer would mean money movement asserting a logistics fact - the axis collapse design 4.4 forbids.
  Design 4.4 now lists this axis as the fulfillment workflow's alone, which is what the code does.
  Do not "close" this by implementing it: an implementation that followed the old sentence would pass
  its tests and silently couple two axes the spec keeps independent.
