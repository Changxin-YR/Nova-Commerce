# Phase 5 handoff - payment (payment-workflow)

Task **t3**, **completed**. **Nothing of mine is uncommitted** (`git status --porcelain` over my
four paths is empty). Green at HEAD: **205 passed**, FG-11 gate **11 passed** (6/6 repeat runs).
Read `PHASE5_DESIGN.md` §2, §4.1, §5.1-5.3, §6.1, §7, §9 before changing anything.

## 1. Files I own

- `payment/providers.py` - `filter_callback_payload()`, `verify_signature()`, `sign_body()`, `parse_amount_minor()`, `callback_secret_for()`. No DB, no ORM.
- `payment/schemas.py` - `CreatePaymentRequest`/`MockPayRequest` (`extra="forbid"`), `PaymentOut`, `PaymentCreateOut`, `CallbackAckOut`.
- `payment/serializers.py` - ORM -> wire; `pay_url_for()` (MOCK only), `to_payment*()`, `to_callback_ack()`.
- `payment/service.py` - `PaymentService`, `canonical_payment_request_hash()`, `is_mock_allowed()`. **Owns the commit.**
- `payment/workflow.py` - `PaymentSuccessWorkflow`, `CallbackRequest`, `CallbackExecution`, `order_deduct_key()`. **Never commits.**
- `payment/api/{__init__,customer,callbacks,admin}.py` - the six frozen `/payments/*` routes; callback takes provider headers, **no JWT**.
- `payment/__init__.py` - public surface, 35 exports; mine per §12.
- `tests/unit/modules/payment/*` - 174 unit tests (providers, schemas, serializers, domain contracts).
- `tests/integration/payment/*` - 20 HTTP tests on real MySQL + self-contained `conftest.py`.
- `tests/concurrency/test_payment_idempotency.py` - FG-11 gate: 11 tests, fixture-free, real threads.

**Not mine:** `payment/{enums,models,repository}.py` + `migrations/versions/**` (data-layer);
`router.py`, `config.py`, `.env.example`, `docs/**`, `scripts/**` (captain).

## 2. Done and committed

- `85e12d7` - providers, schemas, serializers, service, workflow, api/*, all three test dirs incl. the FG-11 gate.
- `9b01fbf` - FG-11 teardown deadlock fix (the gate was flaky 3 runs in 10).
- `fdf488d` - FG-11 fixture's own seed/teardown deadlocked under load: shared `_retry_on_deadlock`, fresh marker per attempt, `_purge_orphan_marker`.
- `be30bec` - callback leak in both teardowns: delete by event-id pattern, not `order_no`.

Evidence: `artifacts/evidence/concurrency/fg11_payment_idempotency.json` = **PASS**, `11 passed`,
`revision dc3e9f8`, captain-emitted. `git diff dc3e9f8 HEAD` over my gate paths was empty when I
checked; re-emit from `be30bec` or later if it is re-run.

## 3. Not done - priority order

1. **Nothing required** - all five t3 deliverables are landed and green.
2. Optional: the shared seed leaks `payment_callbacks` rows (`paid_order()` settles, `purge_shop()`
   never removes the callback); 13/run measured, reported to data-layer with the fix pattern.
   Check: `docker exec nx-mysql mysql -unova -pnova_dev_password nova -N -e "SELECT COUNT(*) FROM payment_callbacks;"`
3. Optional: `import providers` costs ~1 s via the package `__init__` pulling in the ORM.

## 4. Traps that cost real time

- **Attempt the claim, never pre-check it.** `insert_received` wraps the insert in a SAVEPOINT and
  reads the duplicate-key error as "you lost". `SELECT`-then-`INSERT` is check-then-act: two
  deliveries both find nothing and both settle. `uq_payment_callbacks_provider_event` serialises.
- **`after_commit` fires for a SAVEPOINT release too**, so commit counting reports **2 for one
  settlement**. Count real commits via the engine's `ConnectionEvents.commit`; use
  `session.get_nested_transaction()` to tell a savepoint from the root transaction.
- **The router mounts submodules at the module prefix only** (submodule name is just an OpenAPI
  tag), so each sub-router spells its own sub-path. `@router.post("/{provider}")` gave
  `POST /api/v1/payments/MOCK` - registered, plausible, wrong. Frozen-path tests must probe by
  **answer**, not by the route table.
- **Fixture teardown deadlocks under load** (1213/1205 are retryable by contract), and
  `LIKE 'P-FG11-%'` matched *every* run's product rows. Delete by captured primary keys, retry the
  whole transaction, fresh marker per attempt.
- **Teardown must not scope by `order_no`.** A refused delivery resolves no payment, so
  `order_no`/`merchant_id` are NULL on exactly those rows - and `payment_callbacks` has no FK to
  `payments` (§5.2), so they leak permanently. Delete by event-id pattern.

## 5. Interfaces others depend on

`tests/integration/commerce/seed.py` imports five symbols from this module, making these
load-bearing across the payment, fulfilment and after-sales suites. **Do not change either without
telling the fulfilment and after-sales authors.**

```python
PaymentService(session, settings=None).create(
    *, principal: Principal, order_no: str, channel: str,
    client_request_id: str, idempotency_key: str) -> PaymentCreateResult
# PaymentCreateResult(payment, replayed, order); COMMITS. Raises OrderNotFound,
# OrderStateInvalid, PaymentStateInvalid, PaymentChannelUnsupported,
# IdempotencyKeyRequired, IdempotencyPayloadMismatch.

PaymentSuccessWorkflow(session, settings=None).execute(request: CallbackRequest) -> CallbackExecution
# CallbackRequest(provider, event_id, event_type, timestamp, signature, raw_body: bytes, payload)
# CallbackExecution(callback, payment, order, replayed, duplicate, processed, uncommitted,
#                   status, error_code, detail); .applied == owned the effect.
# Commits iff it applied an effect, else rolls back.
```
Also: `providers.sign_body` / `filter_callback_payload`, `enums.PaymentChannel`,
`order_deduct_key(order_no=, order_item_id=)` = `order-deduct:{order_no}:{item_id}`, and
`FulfillmentService.create_shell(order=, items=, warehouse_id=)` (fulfilment's; workflow step 8).

## 6. What I could not verify

- **Flakiness is bounded, not eliminated.** 6/6 gate and 3/3 suite clean after `be30bec`, 12/12
  earlier - but no hundreds-of-iterations or parallel-process run, so a rare deadlock may remain.
  Re-measure; do not trust one green run.
- **The PASS artifact names `dc3e9f8`, not `be30bec`.** Gate paths were unchanged between them; I did
  not re-emit (evidence emission is the captain's by rule).
- **No real non-MOCK provider exercised** (only MOCK exists); a PSP body is untested and §15.7 leaves
  it unfrozen.
- **Whole suite:** last full run `1067 passed, 27 errors`, all 27 in after-sales' setup
  (`NameError: commerce_shop`) - not mine. **Collection works right now:**
  `pytest tests --collect-only -q` -> `1098 tests collected in 1.88s`. Treat any claim that `tests`
  cannot be collected as stale until that command says so.
