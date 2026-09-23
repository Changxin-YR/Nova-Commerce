# HANDOFF — READ THIS FIRST

> **Snapshot:** the commit that last touched this file (see `git log -- frontend/REDESIGN_STATUS.md`).
> Everything below the `---` line is chronological history. This block is the entry point.

## 1. State in one screen

Four gates, all run on this exact tree (literal output):

| Command | Output | Exit |
| --- | --- | --- |
| `npx vue-tsc --noEmit` | *(no output)* | 0 |
| `npx vite build` | `✓ 2386 modules transformed` `✓ built in 1.69s` | 0 |
| `npx vitest run` | `Test Files 21 passed (21)` / `Tests 295 passed (295)` | 0 |
| `npx eslint .` | *(no output)* | 0 |

`git status --porcelain` → empty.

**Done:** all 22 routes, the whole `API_CONTRACT.md` migration (types → availability → views), the
§11 addenda, all 10 console pages on the dense 京麦 pattern, the §12.1 coupon preview→create flow,
**§13.1/§13.2 promotion creation**, and `agentApi.cancelRun` (§4's frozen task endpoint).

**Not done:** **role/permission editing is BLOCKED** — see §3.2, it needs a discovery endpoint the
contract does not freeze. Everything else in §13 is built or has its shapes ready.

## 2. The five console pages

| Page | Status | Actions/fields WIRED | NOT wired |
| --- | --- | --- | --- |
| `console/AfterSalesView.vue` | DONE | list (keyword + status + paging), approve, reject, refund; claim vs money columns | `afterSaleAdminApi.detail` (no detail page); `reject_reason`, `description`, `evidence_urls`, `items[]` never displayed |
| `console/KnowledgeView.vue` | DONE | bases, documents (keyword + paging), upload, **reprocess**, **archive**, retrieval debug, evaluation, PROCESSING auto-poll | `knowledgeAdminApi.createBase` (no UI to create a knowledge base); no document detail view |
| `console/MarketingView.vue` | DONE | coupon list, **coupon preview → create**, promotion list, **publish/unpublish**, **promotion preview → create** (§13.1/§13.2) | `marketingApi.myCoupons` / `claim` (no consumer coupon-center page); no edit/delete for an existing promotion |
| `console/SystemView.vue` | DONE | health `ready`/`live`, dependency table w/ criticality, audit list (filter + paging) | **role/permission editing** (BLOCKED — §3.2: no frozen role/user LIST route); the `system.rolePermissions` / `system.userRoles` endpoints exist in `endpoints.ts` but have **no API function and no UI** |
| `console/AiWorkspaceView.vue` | DONE | agent runs list, **run cancel / cancel-pending-approval** (was a frozen endpoint with no caller), tools, pending actions approve/reject, SSE streaming (5 tabs) | `agentApi.run` (single run detail), `agentApi.threads`, `agentApi.chat` (streaming is used instead) |

The 5 views that were "pre-redesign markup" at the start of t4 all render filter bar → hairline
`.nx-table` → pager, with `StateView` covering all five §108 states, and every row action gated by a
pure tested availability module rather than an inline status check.

## 3. What §13 unblocked, and the one thing it did not

`API_CONTRACT.md` §13 froze the six shapes §12 had left undefined. **All six are now transcribed into
`src/types/frozen-contract.ts` and re-exported from `src/types/domain.ts`** — additively, so nothing
broke. Consuming them is the remaining work:

1. **Promotion creation — DONE** (commit `dfe527c`). `promotion_type` discriminates `rule_config`, so
   the form renders per-type rule fields and sends the matching variant; rates are basis points;
   `scope` is explicit; `preview_token` is required in the type. The duplicate local `Promotion` in
   `src/api/marketing.ts` was **deleted in the same commit**, so there is one shape again.
2. **Role / permission editing — STILL BLOCKED, and this is a contract gap, not a frontend TODO.**

   **Verified by command** (this is the pull-quote; do not take it on trust — re-run it):

   ```
   Select-String -Path C:\Users\27363\Desktop\store\docs\architecture\API_CONTRACT.md `
                 -Pattern 'roles','users'
   -> L92  | Update a role's permissions | PUT /api/v1/system/roles/{id}/permissions | Role |
      L93  | Assign a role to a user    | POST /api/v1/system/users/{user_id}/roles  | User |
   ```

   Two endpoints, **both writes**, and **no LIST route for roles or users** anywhere in the document.

   That matters because `Role.id` is the INPUT to the write: with no way to enumerate roles, the client
   cannot discover an id to update. **This is exactly the discovery gap §5.2 closed for
   fulfillments** — `POST /fulfillments/{id}/ship` needed an id, nothing said how to learn one, and
   `GET /fulfillments/admin` was frozen in response. The role editor needs the same treatment
   (`GET /system/roles`, plus `GET /system/users` for assignment).

   Per the captain's ruling for this flow (do not push with assumed shapes), inventing a list route is
   not an option, so the editor is **unbuilt rather than half-guessed**. Everything else it needs is
   ready: `Role` / `User` / `RolePermission` are transcribed, and `UpdateRolePermissionsRequest` carries
   the complete explicit set plus `reason`. §13.4's four server-side obligations mean the UI must be a
   **review flow, never a toggle grid** — the coupon/promotion preview→confirm pattern is the template.

**MIGRATION HAZARD, read before touching marketing:** `src/api/marketing.ts` still carries a LOCAL,
invented `Promotion` (`type`, `rule: Record<string, unknown>`, `start_at`/`end_at`). §13 supersedes it.
`MarketingView.vue` reads `promotion.type` / `.start_at` / `.end_at`, which become `promotion_type` /
`starts_at` / `ends_at`. Two shapes for one resource is exactly the defect this codebase removed once
for `Order` — delete the local one in the same commit that migrates the view, not after.

## 4. Every assumption made (consolidated)

All of these are §10 territory and are marked as assumptions in the code as well as here.

1. **`AgentRun` / `PendingAction` shapes are NOT frozen** (§10). The AI workspace reads, and therefore
   assumes: `AgentRun` = `id, thread_id, agent_name, status, query, tokens_used, cost_amount,
   started_at, finished_at, pending_action_id, error_code, error_message`; `PendingAction` = `id,
   agent_run_id, action_type, tool_name, summary, risk_level, status, payload, payload_hash, diff,
   requested_by, decided_by, expires_at, created_at`. Documented in a comment block at the top of
   `AiWorkspaceView.vue`; the list is kept deliberately **narrow** so a Phase 10/13 freeze only needs
   that one field list reconciled.
2. **`CouponPreviewResult`** — §12.1 freezes the endpoints, not the `CouponPreview` shape. Narrow
   local view model: `preview_token` (stated as certain by §12.1) plus the echoed request fields.
3. **Promotion shape + lifecycle** — **NO LONGER AN ASSUMPTION.** §13 froze the shape; see §3 above.
   The transitional state is that the frozen `Promotion` type exists but `src/api/marketing.ts` still
   exports a local, invented one, so the two currently coexist. That is a known, tracked migration, not
   a settled design.
4. **Analytics QUERY parameter names** — sent as `from` / `to` / `granularity`, mirroring the `period`
   keys the RESPONSE does freeze. Response envelope is authoritative; only the request side is assumed.
5. **Analytics route** — `/analytics/admin/metrics/{metric}`. §8 froze the envelope and the 5 metric
   names, not the route. One function to change.
6. **Knowledge + promotion transition guards** — the frontend's conservative reading of §53 / §47
   (`PROCESSING` and `ARCHIVED` block both document actions; only DRAFT publishes). The server stays
   authoritative (`DOCUMENT_STATE_INVALID` 100002, `PROMOTION_CONFLICT` 90001) and both views handle
   those codes explicitly.
7. **`InventoryMovement`** — left module-local in `src/api/inventory.ts`; §10 does not freeze it and it
   is display-only (and currently has no UI).

## 5. `refundable_amount` bridge — THREE CONDITIONS (pass this forward)

`API_CONTRACT.md` §11 made `OrderDetail.refundable_amount` server-owned because it is **INV-005 exposed
to the client**, and §15 forbids the client being its authority. The backend order module has not
landed, so `src/domain/orders/availability.ts::refundableAmount()` falls back to
`paid_amount - refunded_amount`. **Deleting the fallback today would silently kill every refund
affordance** (`undefined > 0` is `false`), so it is kept under three conditions — all three must
survive:

1. **It SPEAKS** — a dev-only `console.warn` naming the missing field and the removal trigger, guarded
   by `import.meta.env.DEV` (verified tree-shaken: the text is absent from `dist/assets/*.js` after a
   real build). Warns once per session.
2. **It is NAMED** — `@deprecated` JSDoc on `refundableAmount` plus `TODO(phase-5)` at the fallback.
3. **Deletion has an OWNER** — Phase 5's definition of done in `HANDOFF.md`. **Verify this is still
   there:** as of `e34ee66` a grep of `HANDOFF.md` for `refundable_amount`/`bridge` returned nothing.

Three tests pin it: the fallback warns; the server path does **not** warn (or the signal stops meaning
anything); it warns only once per session. **When Phase 5 lands the field, remove the fallback AND those
fallback spec cases — do not leave a silent second source of truth for money in the domain layer.**

## 6. Next steps (in priority order)

1. **Role / permission editing — needs a discovery endpoint first.** Ask for `GET /system/roles`
   (and `GET /system/users` for assignment) to be frozen, on the §5.2 precedent. Everything else is
   ready; see §3.2.
2. **Wire `knowledgeAdminApi.createBase`.** The Knowledge page can list bases, upload documents,
   reprocess and archive — but cannot CREATE a base, so on a fresh deployment the page has nothing to
   operate on and no way to fix it from the UI. Smallest remaining user-visible hole.
3. **Wire the rest of the frozen-but-unused surface** if wanted: `inventoryAdminApi.movements`,
   `catalogApi.categories` / `brands`, `catalogAdminApi.upsertSku`, `governanceApi.pendingAction`,
   `agentApi.run` / `threads`. *(Measured by script audit of `src/api` exports against all `src/views` +
   `src/components` + `src/stores` references; re-run it after any wiring — it is the cheapest way to
   find a frozen endpoint with no caller, which is how `agentApi.cancelRun` was found.)*
4. **Retire the `.nx-card` / `.nx-pill` compatibility aliases** once nothing references them
   (73 + 17 occurrences across 15 files at last count) — cosmetic, not a defect.
5. **Delete the home-floor preview block** (`tags: ['预览数据']`) in `HomeView.vue` once the catalog
   module answers for real.

## 7. Problems found but NOT handled

- ~~**`agentApi.cancelRun` has no UI**~~ — **FIXED.** Found by script audit and wired with a tested
  availability module (`src/domain/agent/availability.ts`, 7 tests: a run is cancellable while `RUNNING`
  or `WAITING_APPROVAL`, never once terminal). Keep auditing: it is the only method that found a frozen
  endpoint with no caller.
- **Persistent test-noise:** `user-event`/`jsdom` emit `Failed to resolve component` warnings in some
  view specs; harmless, but a genuinely missing global component registration would be hidden by them.
  Not investigated.
- **No e2e coverage of the new pages.** `playwright` has 5 specs from the t1 scaffold; none exercise
  AfterSales / Knowledge / Marketing / System / AiWorkspace. The gates above are unit + build only, so
  "the API contract is honoured" is verified at the type and module-mock level, **not** against a live
  backend.
- **No page has been exercised against a real backend.** All 12 `app/modules/*` APIs are absent, so
  every list in this app currently renders the Empty/Error state against a live server. Everything
  verified here is verified against module mocks and the OpenAPI document.
- **`RequestLog`/audit `before_snapshot`/`after_snapshot` are never rendered** in `SystemView`; §133
  masking is a server concern, but the UI shows none of it.

## 8. Rule carried forward for reports

**Every factual statement in a report must either carry the command output that proves it, or be
explicitly marked as unverified.** This project's hardest bugs were all "plausible and wrong": a nested
`snapshot` shape, `order.status` being `undefined` (which silently hides every action without
throwing), `SalesTrend.money: boolean` making the unit a guess, and a client-derived `refundable_amount`
that would disable refund controls silently. None of them would have failed a build. State the evidence
or label the guess.

---

# Frontend redesign — migration status

## Phase 8 (t2) — Merchant console

### DONE and verified

**Action-availability logic (the t2 test requirement)** — pure modules under `src/domain/`,
all wired to the frozen state machines and unit-tested:
- `orders/availability.ts` — 28 tests. Encodes §31 ("shipping NEVER changes `order_status`",
  so an order is PROCESSING while shipped), refusal after partial shipment, and the rule that
  the ship action requires a fulfillment id.
- `afterSales/availability.ts` + `inventory/availability.ts` + `governance/availability.ts` —
  20 tests. Refund cap = `approved_amount || requested_amount − refunded_amount`; optimistic-lock
  version required; only PENDING pending-actions are decidable; `READ` is the LOWEST risk level.
- `listParams.ts` — 21 tests. Empty select → absent key (not `status=`), page ≥ 1, page_size
  capped, whitespace-only text treated as absent.

**Console pages rebuilt to the dense pattern**: `OrdersView.vue` (filter bar → hairline table →
pager, row actions from the availability module, ship dialog that resolves a fulfillment id
first) and `ProductsView.vue` (task-based publish/unpublish).

**API-boundary mock test** — `views/console/__tests__/OrdersView.spec.ts` (11 tests):
`vi.mock('@/api')`, asserting which action renders per status, that each action calls the
frozen task endpoint function, that shipping calls `fulfillmentAdminApi.ship(fulfillmentId, …)`
with only the three accepted fields (§110), and that a 403 is handled gracefully.

**Two real bugs found by these tests, both fixed:**
1. `useAsyncState` only detected a **bare** empty array, so a paged `{items: [], meta}` payload
   rendered an empty table + "共 0 条" pager instead of the §108 Empty state — on every list
   page. Fixed centrally (`isEmptyPaged`) and covered by 13 new tests.
2. Shipping was **unreachable**: the endpoint is fulfillment-keyed, the id was fetched only
   inside the click handler, and hiding the action until an id exists left nothing to click.
   Fixed with `canResolveShipment()` so a shippable order always has a way in.

**Endpoint paths realigned to the frozen `task_endpoints` list** in `PROJECT_BASELINE.yaml`
(3 were wrong in my t1 scaffold — see the t2 report for the divergence list).

### REMAINING — the other 8 console pages

> **SUPERSEDED for the migrated pages.** `InventoryView`, `DashboardView` and `AnalyticsView` were
> subsequently moved to the frozen contract (see "Frontend contract migration — DONE" at the end of
> this file). `AfterSalesView`, `MarketingView`, `AiWorkspaceView`, `KnowledgeView` and
> `SystemView` are still NOT wired to their availability modules — their contract migration status
> is unchanged by the order/inventory/analytics work.

Still on the pre-redesign markup (they render correctly through the compatibility aliases but
are not dense tables, and are not wired to the availability modules):

| Page | Needs |
| --- | --- |
| `AfterSalesView.vue` | dense table + `afterSaleActionFlags` / `validateRefundAmount` |
| `MarketingView.vue` | coupon/promotion tables (no frozen task endpoints exist — see report) |
| `AiWorkspaceView.vue` | dense message blocks + pending-actions table via `pendingActionFlags` |
| `KnowledgeView.vue` | doc table + `buildKnowledgeDocParams` + retrieval stages |
| `SystemView.vue` | health dependency table (criticality column) |

Plus: 7 files still use `formatMoney` instead of `<PriceText>` (amounts correct, uniformity
pending), listed below.

---

## Brand rename (owner decision)

Done in `frontend/` only. Two case-sensitive passes over **68 occurrences across 15
source/config/doc files**: package name, `index.html` title, `APP_TITLE`/`APP_SHORT_NAME`, the
five `localStorage` keys, the console log prefix, the client class/type identifiers
(`NovaHttpClient` / `NovaExtras` / `NovaRequestConfig` / `NovaClientOptions`) plus their spec
references, the CSS doc header, and all user-facing copy.

**Verified by the owner's grep command returning EMPTY** — 0 matches for the retired brand
across `*.ts,*.vue,*.json,*.html,*.md,*.css,*.scss,*.mjs,*.cjs` (excluding node_modules/dist) —
with `Nova` present at 66 occurrences, including the `index.html` title, `package.json` name
and the e2e title assertion. No dangling references to the old identifiers remain.

Deliberately NOT renamed, with reasons:
- `nx-*` / `--nx-*` (627 usages) — our own CSS namespace; never contained the brand name.
  Renaming would be churn with real regression risk and no user-visible benefit.
- `cookie?: never` in `src/types/generated/api.d.ts` — the HTTP `Cookie` header from the
  OpenAPI document, not a brand string. Regenerated by `npm run gen:api`.
- Third-party `js-cookie` / `tough-cookie` package entries in the lockfile.

`dist/` was **deleted and rebuilt**, not hand-patched. `npm install` re-synced
`package-lock.json` (`nova-frontend`).

**Deploy consequence:** the `localStorage` key rename logs out any browser holding a token
under the old keys, once (documented in README).

## Price + table unification (item 5)

- **`formatMoney` → `<PriceText>`: COMPLETE.** 0 call sites and 0 imports remain in
  `src/views` + `src/components`; `<PriceText>` appears 69 times. The only remaining
  `formatMoney` caller is the notification path, which cannot interpolate a component — those
  messages now quote integer minor units with an explicit `分` suffix so no value is formatted
  by hand.
- **Console tables: unified.** All 11 console `<table>` elements now use the shared
  `.nx-table` primitive (was 7 pages with bespoke `xx__table` classes + duplicated CSS, now
  removed). Consistent cell padding, tinted header with column separators, row hover, tabular
  figures.
- **Still open:** 73 `nx-card` + 17 `nx-pill` references across 15 files. These are
  compatibility aliases that already render the dense look, so this is markup tidiness, not a
  visual defect. Remaining files: `AiWorkspaceView`, `AnalyticsView`, `DashboardView`,
  `InventoryView`, `KnowledgeView`, `MarketingView`, `AfterSalesView` (both), `SystemView`,
  `AddressesView`, `AfterSaleDetailView`, `LoginView`, `MockPayView`, `OrderDetailView`,
  `ProfileView`. Also pending: dense `.nx-filterbar` + `.nx-input` on the 7 console pages whose
  filters are still plain inputs.

---

Design direction: information-dense Chinese B2C commerce (tight type, 1px hairlines,
~0 radius, grey page / white blocks, red pricing). Element Plus is themed via CSS custom
properties only; no component library was swapped (§6).

## Done — verified by gates (typecheck / build / vitest / lint all 0)

**Theme surface** — `src/styles/tokens.scss`
- 61 `--el-*` Element Plus variables overridden in ONE block (inventory in README).
- `--nx-*` brand tokens: brand/price red `#e1251b`, link blue `#1d7de0`, text
  `#333/#666/#999`, border `#e8e8e8`, page `#f5f5f5`, radius `2px`, 12px base, 1190px container.
- Structural primitives: `.nx-block`, `.nx-floor-title`, `.nx-table`, `.nx-filterbar`,
  `.nx-tabs`/`.nx-tab`, `.nx-badge`, `.nx-btn` (`--primary` solid / `--outline` orange),
  `.nx-rows`.
- Section 8b holds **compatibility aliases** (`.nx-card`, `.nx-page-title`,
  `.nx-section-title`, `.nx-pills`/`.nx-pill`) that map the pre-redesign markup onto the new
  dense look, plus the legacy `--nx-primary*` aliases (an undefined custom property would
  silently fall back to the inherited colour). Retire each alias once nothing references it.

**`<PriceText>`** — `src/components/ui/PriceText.vue` + 12 tests
- The single price renderer: `¥ 2,999 .00` with a small symbol, large bold tabular integer,
  small cents. Truncates (never rounds) when the cents are hidden.
- Minor→major conversion lives ONLY in `utils/money.ts::splitMoney()`.

**Redesigned components/views**
- `layouts/StoreLayout.vue` — 4 bands: utility bar → logo + centred search + cart →
  category strip w/ hover panel → service promises + footer.
- `layouts/ConsoleLayout.vue` — dark fixed left menu (grouped), breadcrumb topbar, dense content.
- `views/consumer/HomeView.vue` — category rail + CSS-gradient carousel + user/services panel
  + tabbed floors + ranked list. Banners are pure CSS with our own copy.
- `views/consumer/ProductView.vue` — gallery + buy box, SKU chip grid with explicit
  out-of-stock state, quantity stepper, dual CTA (orange outline + solid red).
- `views/consumer/CartView.vue` — hairline table + steppers + sticky settlement bar.
- `views/consumer/CheckoutView.vue` — address → payment channel → item table → sticky
  amount panel with per-line breakdown.
- `views/consumer/OrdersView.vue` — status tabs whose VALUES are our frozen `OrderStatus`
  enum (labels are our copy; no borrowed status vocabulary).
- `views/console/ProductsView.vue` — the reference dense console table (filter bar → table → pager).
- `components/ui/ProductCard.vue`, `StatusChip.vue`, `StateView.vue`, `MessageBlocks.vue` —
  restyled; Metric now has a real numeric hierarchy (12px label → 24px tabular value), tables
  are operations-dense.

## Remaining (the aliases keep these visually coherent meanwhile)

1. **Pricing not yet via `<PriceText>` in the views not covered by the contract migration.**
   The migrated pages (`console/{Dashboard,Analytics,Inventory,Orders}`, `consumer/{Orders,OrderDetail}`)
   now render every amount through `<PriceText>` or the unit-aware analytics formatter. The
   remaining files listed below still use `formatMoney`; they render correct amounts, so this is a
   uniformity gap, not a bug.
2. **Console/secondary views still use the `.nx-card` + `.nx-pills` aliases** instead of the
   dense `.nx-block` / `.nx-table` / `.nx-tabs` markup:
   `console/{DashboardView,AnalyticsView,InventoryView,OrdersView,AfterSalesView,MarketingView,KnowledgeView,SystemView,AiWorkspaceView}.vue`,
   `consumer/{SearchView,AddressesView,ProfileView,LoginView,AfterSalesView,AfterSaleDetailView,MockPayView,OrderDetailView,AssistantView}.vue`.
   They render the new look through the aliases; converting them buys consistency, not fixes.

## Not possible yet (needs backend)

The 12 `app/modules/*` APIs do not exist, so no end-to-end data flow is provable. The home
floor shows clearly-labelled local preview rows (`tags: ['预览数据']`) ONLY when the catalog
returns nothing, so the layout is reviewable without passing preview data off as server data.
Delete that block once the catalog module lands.

## API contract reconciliation (API_CONTRACT.md) — MIGRATION COMPLETE

The contract is now frozen. Reading it revealed that **my invented shapes were wrong**, not merely
unconfirmed. That changes the remaining work from "8 pages" to "fix the foundation, then 8 pages".

### What was wrong (mine vs frozen)

| Area | My invented shape | Frozen contract |
|---|---|---|
| Order state field | `status` | `order_status` |
| Order amounts | nested `snapshot.{items_amount,discount_amount,payable_amount}` | FLAT: `original_amount`, `promotion_discount_amount`, `coupon_discount_amount`, `shipping_amount`, `payable_amount` |
| Order line | `{product_title, cover_url, sku_specs, subtotal_amount}` | `{product_name, sku_name, image_url, unit_price, original_amount, payable_amount, allocated_discount_amount, ...}` |
| List vs detail | one `Order` with nested items | `OrderSummary` (no items/shipments) vs `OrderDetail` (+ `items[]`, `shipments[]`) |
| Receiver | `snapshot.receiver_name` unmasked | `receiver_name`/`receiver_phone` **already masked** (§94 — must NOT be un-masked) |
| Fulfillment | `Shipment` with `id: string`, `items[{order_item_id,quantity}]` | `Fulfillment` with **`id: number`**, `fulfillment_no`, `carrier`/**tracking `null` until shipped**, `items[{id,order_item_id,sku_id,product_name,sku_name,quantity}]` |
| Ship body | `{carrier, tracking_no, items[], idempotency_key}` | **exactly** `{carrier, tracking_no, item_quantities[]}` — no idempotency key (§110 guard) |
| `carrier` | free text input | a carrier **CODE** (e.g. `"SF"`) |
| Inventory | `{on_hand, reserved, version}` | `{on_hand_qty, available_qty, locked_qty, safety_stock, sellable_qty, sku_no, product_name, sku_name, version}` |
| Inventory adjust body | `{delta, reason, version, idempotency_key}` | `{warehouse_id, sku_id, version, delta_available, reason}` |
| Analytics | `{points:[{date,value}], money:boolean}` | `{metric, unit, period, series[{bucket,value}], summary{total,average,change_ratio}, dimensions[]}` |
| Stale version | generic 409 | 409 + code 40002 with **`data` carrying the current Inventory** |

### Done this turn

- **`src/types/frozen-contract.ts`** — every frozen shape transcribed: `Paged<T>`/`PageMeta`/`emptyPage()`,
  `Fulfillment`, `ShipFulfillmentRequest`, `OrderSummary`/`OrderDetail`/`OrderItem`, `Inventory`,
  `AdjustmentPreview`, `CreateAdjustmentRequest`, `StaleVersionConflict`, and the full analytics envelope
  with `AnalyticsUnit`. Additive, so nothing broke.
- **`src/domain/analytics/unit.ts`** + **21 tests** — the unit-aware renderer. The captain's warning is
  encoded as a test: `refund.rate = 0.12` must render `12%`, never `¥0.12`, and `order_count = 137`
  never `¥1.37`. `minor_currency` is the ONLY branch that returns money, and it returns the raw
  INTEGER for `<PriceText>` rather than a formatted string, so no float touches a monetary value.
  Also `checkMetricUnit()` cross-checks the server's declared unit against the 5 frozen metrics.
- **Endpoint corrections** (ratified by the captain): `/orders/admin` + `/orders/admin/{order_no}`
  (drops the worse `/orders/admin/orders` duplication); added `GET /fulfillments/admin`; `ship`
  now takes `number | string`.

### THE MIGRATION — COMPLETE (types -> availability -> views)

Every consumer of the old `Order`/`Fulfillment` shape moved to the frozen one. **All of the
following is DONE** — kept as the checklist it was executed against; the verified result, with gate
output, is in the "Frontend contract migration — DONE" section at the end of this file:

**Types/API layer**
- `src/types/domain.ts` — `Order`, `OrderItem`, `OrderSnapshot`, `Shipment` become aliases/re-exports of
  the frozen types (or are deleted), so there is ONE shape per resource.
- `src/api/order.ts`, `src/api/aftersales.ts`, `src/api/analytics.ts`, `src/api/inventory.ts` — return
  types and param names (`delta` → `delta_available`, etc.).

**Availability modules + their tests**
- `orders/availability.ts` reads `order.status` → `order.order_status`; `canShipOrder` should take a
  `Fulfillment` (numeric id) and check `carrier/tracking_no === null` for "not yet shipped".
- `inventory/availability.ts` — `on_hand`/`reserved` → `on_hand_qty`/`locked_qty`, and use the
  server's `sellable_qty` instead of recomputing it.
- Their 69 tests change in lockstep (that is the point of having them).

**Views**
- `console/OrdersView.vue` — flat amounts, `order_status`, masked receiver, and ship via the
  fulfillment queue (`GET /fulfillments/admin`) keyed by numeric id with a carrier CODE select.
- `console/InventoryView.vue`, `console/AnalyticsView.vue`, `console/DashboardView.vue` — frozen
  inventory fields and the analytics envelope.
- `consumer/{OrdersView,OrderDetailView,CheckoutView,MockPayView}.vue` — same order-shape migration.

**Recommended order**: types → API modules → availability modules + tests → views. Doing views first
would mean editing them twice.

---

## Frontend contract migration — DONE (verified by gates)

Migrated in the order the plan required — **(a) types -> (b) availability modules -> (c) views** —
because doing views first means writing every view twice.

**Gates, all four re-run on the migrated tree:**

| Command | Result |
| --- | --- |
| `npx vue-tsc --noEmit` | EXIT=0 |
| `npx vite build` | EXIT=0, `2381 modules transformed`, `built in 1.48s` |
| `npx vitest run` | EXIT=0, `Test Files 17 passed (17)`, **`Tests 246 passed (246)`** (floor was 230) |
| `npx eslint .` | EXIT=0 |

### (a) Types + API layer — ONE shape per resource

- **DELETED from `src/types/domain.ts`**: the invented `Order`, `OrderItem`, `OrderSnapshot` and
  `Shipment`. They are gone rather than kept beside the frozen types, because two shapes for one
  resource is how a silent integration bug starts — half the app keeps compiling against the old
  one. `domain.ts` now **re-exports** the frozen shapes, and `OrderDetail as Order`, so
  `import type { Order } from '@/types/domain'` still resolves to exactly one definition.
- **DELETED from `src/types/api-contract.ts`**: `ShipRequest` (the endpoint accepts no
  `idempotency_key`), and `AnalyticsOverview` / `SeriesPoint` / `SalesTrend` / `TopProduct` /
  `OrderFunnelStage`. `SalesTrend.money: boolean` was the dangerous one: it made the unit a guess.
- `src/api/order.ts`: `list` returns `Paged<OrderSummary>` (NOT a bare array), `detail` returns
  `OrderDetail`, `ship` takes `number | string` and `ShipFulfillmentRequest`, and
  `fulfillmentAdminApi.list` (the §5.2 queue) was added.
- `src/api/inventory.ts`: `stock` returns `Paged<Inventory>`; `adjust` is no longer sku-keyed and
  sends `{warehouse_id, sku_id, version, delta_available, reason}`; `preview` added.
- `src/api/analytics.ts`: one `metric(metric, query)` call returning `AnalyticsEnvelope`.
- `src/api/endpoints.ts`: `/inventory/adjustments`, `/inventory/adjustments/preview`,
  `/analytics/admin/metrics/{metric}`.

### (b) Availability modules — the rules the captain called out

- `orders/availability.ts` reads **`order_status`** everywhere (the rename is a correctness fix:
  `order.status` is `undefined`, so every `includes()` check returned false and the console would
  have hidden EVERY action without throwing).
- `canShipOrder(order, fulfillment)` now takes a **`Fulfillment`** and decides "not yet shipped"
  from **`carrier === null`**, exposed as `isUnshippedFulfillment()`. A package that already carries
  a tracking number can never be offered for shipping again (the server answers 70 003).
- `inventory/availability.ts` uses the server's **`sellable_qty`** and never recomputes it. The
  tests deliberately set `sellable_qty` inconsistent with `on_hand_qty - locked_qty`, so a
  recomputing implementation fails them.
- `refundableAmount(order)` = `paid_amount - refunded_amount`, derived in ONE place, because the
  frozen payload has no `refundable_amount` (`undefined > 0` is `false`, which would silently
  disable refunds).
- Tests migrated in lockstep: **246 tests total**, including new coverage for `carrier === null`,
  `findUnshippedFulfillment`, the derived refundable amount, and server-vs-recomputed sellable stock.

### (c) Views

| View | What changed |
| --- | --- |
| `console/OrdersView.vue` | Rows are `OrderSummary`; fulfillment ids come from the **queue** (`GET /fulfillments/admin`) instead of N per-row detail calls; carrier is a CODE `<select>`; the ship dialog sends exactly three fields and **no** `idempotency_key`. |
| `console/InventoryView.vue` | `Inventory` fields (`sku_no` / `product_name` / `sku_name` / `on_hand_qty` / `locked_qty` / `sellable_qty`); adjust sends `delta_available` as a body field; live validation reason in the dialog. |
| `console/DashboardView.vue` | Five metric calls, one envelope each; KPI goes through the unit-aware formatter (`minor_currency` -> `<PriceText>`, `count`/`ratio` -> text); chart converts minor->major only at the render boundary. |
| `console/AnalyticsView.vue` | Three envelopes through one `specFromEnvelope()` helper; the invented `points`/`money` shapes are gone. |
| `consumer/OrdersView.vue` | `Paged<OrderSummary>` (`?.items`), `order_status`, flat amounts, derived refundable. |
| `consumer/OrderDetailView.vue` | `order.items` (not `snapshot.items`), flat amounts, masked receiver shown as-is (§94), derived refundable. |
| `consumer/AfterSalesView.vue` | `order.items`, derived refundable. |
| `console/__tests__/OrdersView.spec.ts` | Rebuilt on frozen fixtures + the queue mock; asserts the numeric fulfillment id and that the payload has exactly `carrier` / `item_quantities` / `tracking_no`. |

### Two contract gaps found (NOT guessed)

1. **`OrderSummary` carries no line items.** The console orders table and the consumer orders list
   both rendered `order.snapshot.items`. On a frozen `OrderSummary` that is `undefined`; rendering
   real lines needs one detail call PER ROW. Both views now show order-level figures and route to
   the detail payload (which has `items[]`). If the list should show goods, the contract needs a
   summary field (e.g. item count / first product name) — that is a backend decision, not a frontend
   guess.
2. **`cancel_reason` does not exist** on `OrderSummary`/`OrderDetail`, and `API_CONTRACT.md` §10
   does not list it as intentionally unfrozen either. The "取消原因" lines were removed rather than
   kept as decoration over a value the server never sends, and replaced with a comment at each site.

### Assumptions (API_CONTRACT.md §10 leaves these open)

- **Analytics query parameter names**: the window is sent as `from` / `to` / `granularity`,
  mirroring the `period` keys the response DOES freeze. Only the request side is assumed; the
  response envelope is authoritative.
- **Analytics route**: a metric path segment at `/analytics/admin/metrics/{metric}`. §8 froze the
  envelope and the five metric NAMES, not the route. Changing this touches one function.
- **Inventory movement shape** (`InventoryMovement`) stays module-local: §10 does not freeze it and
  it is display-only.

---

## CONTRACT ADDENDA CONSUMED (§11, commit ffbd14f)

`API_CONTRACT.md` §11 added the three fields this migration had reported as gaps. All three are now
consumed rather than worked around — the workarounds existed only because the fields were undefined,
and the contract is the source of truth, so leaving them in place would have been a stale
degradation.

| Addendum | Where consumed | What it replaced |
| --- | --- | --- |
| `OrderSummary.item_count` + `first_item_name` | `console/OrdersView` goods column, `consumer/OrdersView` items column | A blank/"查看明细" placeholder. Both lists can now name a product with **no N+1 detail fetch** — the `orderAdminApi.detail` function is not even provided by the module mock, so a regression would throw instead of quietly succeeding. |
| `OrderDetail.cancel_reason` | `consumer/OrderDetailView` "取消原因" line | The line was REMOVED during the migration because the field did not exist. It is restored, guarded on `order_status === 'CANCELLED'`, because this page holds an `OrderDetail`. The consumer LIST still cannot show it — it holds `OrderSummary`, a payload distinction rather than a missing field. |
| `OrderDetail.refundable_amount` | `refundableAmount()` in `domain/orders/availability`, consumed by `canRefundOrder` and all three refund surfaces | Client-side `paid_amount - refunded_amount` arithmetic. |

### `refundable_amount` is now read from the server, with a documented bridge

The addendum's reasoning is enforced in code: `refundableAmount()` returns
`order.refundable_amount` when the server supplies it, so the client is no longer the authority on a
figure that gates whether a refund control renders (§15 / INV-005). A derivation remains **only** as
a fallback for a payload produced before the backend order module lands — without it,
`undefined > 0` is `false` and every refund affordance would silently disappear, which is the exact
failure this addendum was written to prevent. It is labelled a transition, not a second source of
truth, and the tests pin both branches: the server figure wins when the two disagree, and the bridge
still yields a usable amount when the field is absent.

### Gates after consuming the addenda

| Command | Result |
| --- | --- |
| `npx vue-tsc --noEmit` | EXIT=0 |
| `npx vite build` | EXIT=0 |
| `npx vitest run` | EXIT=0, **250 passed** (was 246; +4 for the addendum branches) |
| `npx eslint .` | EXIT=0 |

---

## t4 console sweep - all five remaining views converted

`AfterSales`, `Knowledge`, `Marketing`, `System` and `AiWorkspace` are now on the dense 京麦 pattern:
filter bar -> hairline `.nx-table` -> pager, `StateView` for all five section 108 states, and every
row action gated by a pure, unit-tested availability module instead of an inline status comparison.

### New availability modules (pure + tested)
- `src/domain/knowledge/availability.ts` + **15 tests**. `PROCESSING` blocks BOTH task actions (a
  parse/embed job is in flight, so reprocessing queues a second job over the same document and
  archiving pulls it out from under a job about to write chunks); `ARCHIVED` blocks both (a
  reprocess would silently resurrect it); `UPLOADED` offers archive only. A test asserts the terminal
  states genuinely offer nothing, so the hint can never hide a usable action.
- `src/domain/marketing/availability.ts` + **9 tests**. Only a `DRAFT` publishes, only an `ACTIVE`
  promotion unpublishes, `ENDED` is terminal; a test asserts exactly ONE transition per non-terminal
  state so a "simplification" offering both or neither fails.

### Two task paths were WRONG against API_CONTRACT.md section 4
- Knowledge `reprocess`/`archive` were at `/knowledge/admin/documents/{id}/...`; the frozen paths are
  `/knowledge/documents/{id}/reprocess|archive`. Fixed.
- Marketing had **no** promotion publish/unpublish at all, despite section 4 freezing
  `/marketing/promotions/{id}/publish|unpublish`. Added to `endpoints.ts` + `marketingAdminApi`.

### Domain decisions worth keeping
- **AfterSales**: the business claim (section 34) and the money fact (the `refunds[]` ledger, section
  46) render as **separate columns**, and the money column reads the LEDGER rather than the claim -
  collapsing them would report a claim as "refunded" when no money has moved.
- **Marketing**: coupon creation is **preview -> confirm** (section 47). The preview renders the exact
  payload that would be sent, built once so preview and submit cannot diverge, and nothing is posted
  from the form step.
- **System**: `criticality` stays a first-class column with an explicit statement when a `critical`
  dependency is down versus a degradable feature gap.
- **AiWorkspace**: pending-action decidability now comes from the tested `pendingActionFlags` instead
  of inline `status === 'PENDING'` checks.

### Assumptions to confirm (API_CONTRACT.md section 10 territory)
1. **`AgentRun` / `PendingAction` field shapes** are NOT frozen. The fields the AI workspace list
   reads are documented as assumptions in a comment block at the top of `AiWorkspaceView.vue`, and the
   list is kept deliberately NARROW so a Phase 10/13 freeze only requires reconciling that list.
2. **Promotion shape and lifecycle** (`DRAFT/ACTIVE/ENDED`, the local `Promotion` interface in
   `src/api/marketing.ts`) - unfrozen, so both the vocabulary and the transitions are assumptions.
3. **The knowledge and promotion transition guards** are the frontend's conservative reading of
   sections 53 / 47. The server stays authoritative (`DOCUMENT_STATE_INVALID` 100002,
   `PROMOTION_CONFLICT` 90001) and the views handle those codes explicitly.

### Not built, because no endpoint is frozen (reported, not invented)
- **Promotion creation.** Section 47 requires a preview step and `PROMOTION_PREVIEW_REQUIRED` (90003)
  exists as a code, but neither a preview nor a create endpoint is frozen, so the form was not built.
  The page offers status transitions only.
- **Role / permission editing.** Section 65 forbids a CRITICAL write being downgradeable to READ by an
  ordinary console user; a role editor is exactly that kind of write and has no frozen endpoint. The
  System page states the boundary rather than shipping a control that cannot honour it.

### Gates at the end of t4
| Command | Result |
| --- | --- |
| `npx vue-tsc --noEmit` | EXIT=0 |
| `npx vite build` | EXIT=0, `2384 modules transformed` |
| `npx vitest run` | EXIT=0, `19 files`, **`276 passed`** (floor 246) |
| `npx eslint .` | EXIT=0 |

---

## Refundable bridge: the three conditions (captain ruling)

The bridge is KEPT — deleting it before the backend ships `OrderDetail.refundable_amount` would
replace a dead UI with a harder-to-find failure, and a dead UI at least gets debugged. But a second
source of truth for a money figure in the DOMAIN layer is the shape section 15 forbids, so it is
tolerated only while it is labelled, noisy, and dated.

| Condition | Status |
| --- | --- |
| 1. It must SPEAK | DONE. Dev-only `console.warn` naming the missing field and the removal trigger. Guarded by `import.meta.env.DEV` — **verified by absence**: the warning text does not appear in `dist/assets/*.js` after a real `vite build`, so it is tree-shaken from production. Warns once per session, because the function is called from computed values and templates and a per-call warning would flood the console. |
| 2. It must be NAMED for what it is | DONE. `@deprecated` JSDoc on `refundableAmount` + `TODO(phase-5)` at the fallback branch, both stating the removal trigger. Signature unchanged on purpose: the call sites are meant to keep reading the same name. |
| 3. Deletion must have an OWNER | The captain owns this in `HANDOFF.md` (outside `frontend/`). **NOT YET PRESENT** as of commit `0c26686` — a grep of `HANDOFF.md` for `refundable_amount`/`bridge` returns nothing. Flagged, because an unowned TODO is exactly the permanent comment the condition exists to prevent. |

Three tests pin the behaviour: the fallback warns and names both the field and the trigger; the server
path does NOT warn (so the signal stays meaningful); and it warns only once per session.

## Second contract batch consumed (§12.1) and what it still leaves open

`API_CONTRACT.md` section 12 froze promotion/coupon creation and system management. Coupon creation is
now wired to the real endpoints: `POST /marketing/coupons/preview` then `POST /marketing/coupons`,
carrying the `preview_token` the preview returned.

**Preview-then-confirm is now a COMPILE-TIME property**, not a convention:
`CouponCreatePayload` requires `preview_token`, so a single-submit create flow cannot be written
against the signature. Editing after a preview discards the token, because the approval covers the
values the operator actually saw. Six tests assert the flow property rather than a layout.

**A third wrong path, same class as the knowledge one:** `createCoupon` posted to
`/marketing/admin/coupons`, which is only the LIST route; section 12.1 freezes creation at
`/marketing/coupons`. The route existed and was simply the wrong one, so nothing failed loudly.

### Still not built, and why (§12.3's pattern repeats for SHAPES)

section 12.3 names the pattern "the rule was frozen and the interface was not". Batch 2 freezes the
**paths** but still defines no **response shape** for any of the new endpoints:

| Frozen path | Response named in §4 | Shape defined anywhere? |
| --- | --- | --- |
| `POST /marketing/promotions/preview` | `PromotionPreview` | NO |
| `POST /marketing/promotions` | `Promotion` | NO (`Promotion.rule` is a local assumption) |
| `POST /marketing/coupons/preview` | `CouponPreview` | NO — narrow local view model used, marked as an assumption |
| `POST /marketing/coupons` | `CouponTemplate` | NO |
| `PUT /system/roles/{id}/permissions` | `Role` | NO |
| `POST /system/users/{user_id}/roles` | `User` | NO |

So two flows remain unbuildable without guessing:
1. **Promotion creation** — needs the promotion RULE shape; a form cannot be built from
   `Record<string, unknown>`.
2. **Role / permission editing** — needs `Role`/`User` shapes, plus the section 65 semantics (the
   server must refuse a change lowering a write tool's risk level without separate audited approval).
   Building the checkbox UI before that rule has a concrete shape would make the forbidden operation
   one click away again, which is what section 12.2 exists to prevent.

Both are reported rather than filled, per the rule the captain restated twice.
