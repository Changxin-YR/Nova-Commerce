# Nova Commerce continuation — 2026-09-24

This is the current execution handoff. `HANDOFF.md` §19 and
`docs/handoff/phase6-outbox.md` describe the earlier outbox increment and retain
their original measurements; use this file and `FINAL_GATE.md` for current state.

## Verified repository state

- Worktree: `main`; use `git log -1 --oneline` for HEAD and `git status -sb`
  for the current local lead over `origin/main`. No commits from this
  continuation have been pushed.
- Backend: `python -m pytest tests -q` from `backend/` passed **1144 tests** with
  one upstream Starlette/AnyIO deprecation warning. `ruff check --no-cache
  backend/app backend/tests backend/migrations` passed.
- Schema: Alembic head is `48f1e3b5a00c`; `alembic check` found no drift. The
  new `ix_orders_status_expires_at` index was read back as
  `(order_status, expires_at)`.
- `scripts/residue.py` reported zero attributable test rows after the runs.
- Frontend: the recorded FG-03 build passed; FG-20 has 292 Vitest cases and
  FG-21 has 5 Playwright shell cases. These are version-bound in their artifacts.
- `FINAL_GATE.md` currently shows **9 PASS, 17 MISSING**, overall
  **IN PROGRESS**. Each PASS is tied to watched Git paths. It is an evidence
  index, not a claim that the whole product is complete.

## Functionality added in this continuation

1. Outbox publishing now has a Redis Stream transport and a Celery worker/beat
   entry point. A MySQL test makes two publishers race for the same rows; a
   transport test reads the delivered Redis entry.
2. A bounded expired-order reconciliation closes unpaid orders, closes open
   payment attempts, releases every reservation once, and appends a WORKER
   status log in one transaction. It follows the payment-before-order lock
   order used by callbacks. Three real-MySQL tests cover closure, a two-worker
   race, and an unexpired order. Celery beat runs it every 30 seconds.
3. Creating a *new* payment attempt after `orders.expires_at` now answers
   `ORDER_ALREADY_EXPIRED (50005)`. An existing attempt can still be returned
   by the same idempotency input after expiry. The create response now records
   `replayed=True` when the duplicate key was resolved to the existing row.
4. Gate emission was repaired to preserve duplicate pytest test names and
   parametrized names containing spaces. FG-07/08/09/10/11/12 were rerun against
   real tests, and the FG-03/20/21 frontend evidence was added. `FINAL_GATE.md`
   is now generated from the baseline and revision-aware artifacts.

## Remaining work, in execution order

1. **Phase 6 Marketing** (`REQ-MKT-001` through `004`): persist promotions and
   product scope, enforce preview-before-create, resolve active promotion
   strategies in order pricing; implement coupon templates, owned coupons and
   usage records with concurrent lock-on-order, use-on-payment, release-on-cancel.
   The pricing value objects and `PricingRules` seam already exist.
2. **Phase 6 Analytics**: backend metric queries and frozen admin API responses
   are absent. The frontend's marketing/analytics views are present but do not
   prove their backend paths work.
3. **§50 reconciliation**: coupon expiry, expired pending actions, and knowledge
   ingestion recovery remain. Outbox retry and expired-order closure are now
   scheduled. The expired-order batch currently stops on a failed order and a
   consistently failing earliest batch can delay later orders; add per-row
   failure accounting and scan progress when operational policy is defined.
4. **Payment expiry race**: the verified callback path can settle an expired
   `PENDING_PAYMENT` order before the expiry worker closes it. Decide the late
   provider-settlement policy with a reconciliation/refund path before changing
   callback behavior, since provider money may already have moved.
5. **Project gates**: 17 of 26 artifacts are still missing, including mandatory
   FG-13–15, FG-17–18, FG-22 and FG-26. Phase 7–16 features and flagship E2E
   are not complete. `mypy app` reports 85 errors in 20 files; no
   type-clean claim has been made.
6. **Remote sync**: all continuation commits remain local. Push only after the
   intended branch/review path is settled. Re-run affected evidence after each
   watched source change and regenerate `FINAL_GATE.md` at the end.

## Next code entry point

Start with the `PromotionRule`/`CouponRule` types in
`backend/app/modules/pricing/value_objects.py`, the resolver seam in
`backend/app/modules/order/workflow.py` and `backend/app/modules/order/service.py`,
and API_CONTRACT §13. The HTTP layer currently passes no resolved rule, so a
selected `coupon_id` deliberately returns `COUPON_NOT_FOUND (90004)` rather
than silently charging full price. Preserve that guard while introducing the
actual resolver.
