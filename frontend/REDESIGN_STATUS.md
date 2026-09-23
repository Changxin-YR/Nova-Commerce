# Frontend redesign — migration status

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

1. **Pricing not yet via `<PriceText>` in 7 files** (~37 call sites still on `formatMoney`):
   `console/{DashboardView,AnalyticsView,InventoryView,AfterSalesView,MarketingView}.vue`,
   `consumer/{AfterSalesView,AfterSaleDetailView}.vue`.
   They still render correct amounts — this is a uniformity gap, not a bug.
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
