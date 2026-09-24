# Nova Commerce continuation — 2026-09-24

This is the current execution handoff. `HANDOFF.md` §19 and
`docs/handoff/phase6-outbox.md` describe the earlier outbox increment and retain
their original measurements; use this file and `FINAL_GATE.md` for current state.

## Verified repository state

- Worktree: `main`; use `git log -1 --oneline` for HEAD and `git status -sb`
  for the current local lead over `origin/main`. No commits from this
  continuation have been pushed.
- Backend: `python -m pytest tests -q` passed **1165 tests** after the
  product-image increment, including real MySQL and MinIO. One upstream
  Starlette/AnyIO deprecation warning remains.
  `ruff check --no-cache backend/app backend/tests backend/migrations` passed.
- Schema: Alembic head is `f0d29969cfb5`; `alembic check` found no drift.
  The coupon amount columns were read back as BIGINT, and all coupon checks,
  indexes, and foreign keys were read back from MySQL.
- `scripts/residue.py` reported zero attributable test rows after the runs.
- Frontend: **301 Vitest cases**, typecheck, lint, and production build pass.
  The coupon increment also passed the targeted MySQL integration and marketing
  tests (**9 passed**). Refresh source-watched gates after the source commit.
- `FINAL_GATE.md` currently shows **2 PASS, 8 STALE, 16 MISSING**, overall
  **IN PROGRESS**. The latest coupon changes invalidated eight earlier gate
  artifacts; FG-11 and FG-13 still match their watched paths. This is an
  evidence index, not a claim that the whole product is complete.

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
5. Promotions now persist in `promotions` and `promotion_products` with a
   merchant-scoped, payload-bound preview token. Console preview, create,
   publish, unpublish and list endpoints, plus storefront active listing, are
   wired through the real HTTP stack. Preview estimates the 30-day impact and
   reports overlapping scope and time windows. Order preview and create resolve
   the active rule through `PricingService`; order creation reserves the
   promotion quota within its transaction. Real MySQL tests cover the HTTP
   routes, token replay and tampering, scope ownership, pricing, and two
   concurrent orders competing for the last quota slot.
6. Coupon templates, customer coupons, and usage records now persist with
   merchant scope, quota checks, a payload-bound preview token, and database
   constraints. Console preview/create/publish/unpublish/list, customer
   claim/mine, and order preview/create/cancel, expiry worker, and verified
   payment settlement are connected. A coupon locks with its order, is marked
   used only after verified settlement, and is released on cancellation.
   Eight MySQL integration tests cover token replay, concurrency, quota,
   threshold, rollback, payment, HTTP routes, and expiry.
7. The checkout view now sends `items` and `coupon_id` per API_CONTRACT §14,
   and renders the server's separate promotion and coupon amounts. Two view
   tests assert the preview and create payloads.
8. The cart now stores only SKU selection and quantity in user-scoped browser
   storage. It does not call `/cart` or display a client-calculated amount.
   The frontend order paths were corrected to `/orders` and `/orders/preview`.
   Three store tests cover persistence, user separation, malformed data, and
   the path contract.
9. Login, refresh, logout, profile/permission, and owner-scoped address HTTP
   routes now wrap the existing identity services. The refresh token stays in
   a configured HttpOnly Cookie; the client sends it with credentials, stores
   only the short-lived access token, and removes legacy browser token data.
   MySQL HTTP tests cover rotation, cross-origin rejection, logout revocation,
   address ownership, and request validation.
10. The published catalog now serves paged product search, product detail,
    categories, and brands. Listings derive the displayed minimum from active
    SKUs; detail includes server stock and omits private SKU cost. Search and
    detail are checked against a committed MySQL fixture, including the
    unpublished-product boundary. Home and search consume the paged response;
    product selection accepts numeric API SKU IDs. An HTTP purchase test covers
    login -> catalog -> preview -> order -> payment attempt.
11. A new FG-13 emitter runs the existing refresh rotation suite and cookie
    HTTP tests against MySQL at the frozen proof path.
12. Merchant catalog endpoints now list, create, read and edit products, add
    SKUs, and publish/unpublish through the frozen root-level task paths.
    The service checks staff permissions, merchant ownership, category/brand
    ownership and allowed state transitions. A new MySQL HTTP test covers
    creation, publication, storefront visibility, duplicate SKU rejection,
    unauthorized callers and cross-merchant access. Stock still enters through
    inventory operations.
13. Phase 6 Analytics now serves the five frozen metric names from committed
    orders, order-item snapshots, inventory movements, and successful refunds.
    Every response uses the shared metric/unit/period/series/summary/dimensions
    shape, merchant staff scope and `analytics:read`. The console requests all
    five metrics and renders money and rates with their declared units. MySQL
    tests use a real settled order and a real refund workflow; metric definitions
    and zero-denominator behavior are in `docs/architecture/ANALYTICS_METRICS.md`.
14. Merchant staff can upload a validated product image to real MinIO. The
    product-image row saves the returned checksum, MIME type, size and object
    key; setting a primary image demotes the previous one in the same MySQL
    transaction. The storefront returns a signed image URL. A real MySQL/MinIO
    HTTP test checks bytes, metadata, permissions and storefront visibility.
    A frontend control uploads the chosen file and refreshes the product list.
15. Customers can browse coupon templates they can still claim and claim them
    from a new storefront page. The query checks publication, remaining quota,
    expiry, and each customer's claim limit; a MySQL HTTP test proves the list
    changes after a claim. The page also shows owned coupon status.
16. Playwright now drives the checkout page with controlled API responses: it
    selects a coupon, checks the server-supplied payable amount, submits exactly
    one order with matching header/body idempotency keys, creates one payment,
    and reaches the payment page. All six browser cases pass. This exercises
    the browser flow; the API responses in this case are fixtures.

## Remaining work, in execution order

1. **Phase 6 Marketing**: coupon creation, discovery, claim and order lifecycle
   are implemented. Promotion stacking policy and quota release on cancellation
   need explicit business rules before extending the first single-promotion
   implementation.
2. **Phase 6 Analytics**: the five frozen metric endpoints and console reads are
   present. The merchant dashboard still needs an HTTP/browser check with
   representative production-scale data, and the summary comparison convention
   should be reviewed with business stakeholders before relying on it for
   external reporting.
3. **§50 reconciliation**: expired pending actions and knowledge ingestion
   recovery remain. Outbox retry, expired-order closure, and coupon expiry are
   scheduled. The expired-order batch currently stops on a failed order and a
   consistently failing earliest batch can delay later orders; add per-row
   failure accounting and scan progress when operational policy is defined.
4. **Payment expiry race**: the verified callback path can settle an expired
   `PENDING_PAYMENT` order before the expiry worker closes it. Decide the late
   provider-settlement policy with a reconciliation/refund path before changing
   callback behavior, since provider money may already have moved.
5. **Project gates**: 16 of 26 artifacts are still missing, including mandatory
   FG-14–15, FG-17–18, FG-22 and FG-26. Phase 7–16 features and flagship E2E
   are not complete. `mypy app` reports 85 errors in 20 files; no
   type-clean claim has been made.
6. **Remote sync**: all continuation commits remain local. Push only after the
   intended branch/review path is settled. Re-run affected evidence after each
   watched source change and regenerate `FINAL_GATE.md` at the end.
7. **Live route audit**: `create_app().openapi()["paths"]` now lists 65 paths,
   including auth, addresses, catalog, merchant product writes, and Analytics.
   Knowledge, agent, governance, and audit API routers remain
   absent. The HTTP purchase test reaches payment attempt, but an actual
   browser checkout and payment settlement still need E2E verification.
   `PAYMENT_MOCK_ENABLED` is false in the current dev settings, so the mock-pay
   button cannot settle a payment in this environment until the demo profile
   explicitly enables it. The browser checkout case uses controlled API
   responses and does not prove live settlement.

## Next code entry point

Next implement the remaining backend modules and gates, then verify live browser
checkout and settlement with seeded data. Refresh source-watched gate artifacts
after each source increment and regenerate `FINAL_GATE.md`.
