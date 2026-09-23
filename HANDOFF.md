# HANDOFF — Nexora/Nova Commerce V1

> **Read this file first.** It is written for an agent (or engineer) picking this
> project up with **zero prior context**. Everything needed to continue is here
> or is linked from here.
>
> Handoff written: 2026-09-23 08:40 · **Updated 13:14** · Repo `main` at `24cde07`

---

## 0. TL;DR

**What this is:** An AI-native 3C e-commerce operations platform. Two halves that
must stay honest with each other: a real transaction system (inventory, orders,
payment, refund) whose invariants are proven by concurrency tests against real
MySQL, and a *controlled* agent layer that can read facts, analyse, and propose —
but can only write through a tool gateway with human approval.

**Where it stands:** Phases 0-4 complete. **FG-09 (inventory concurrency) and
FG-10 (order workflows, including INV-006/INV-014) both PASS on real MySQL.**
The order API is live, the frontend is well ahead of the backend, and the next
mandatory gates are FG-11/FG-12 in Phase 5.

**Phase 4 is complete; the next task is Phase 5 (Payment + Fulfillment +
AfterSales + Refund) - start with section 17 of this document.**

**The one-line rule that governs everything:** every claim must be backed by
command output that someone else can re-run. Not "it should work" — the spec
(§149) explicitly rejects theoretical completion.

---

## 1. Verify the current state in 5 minutes

> **NOTE (Phase 4 done):** the expected numbers below are from Phase 3. Current expectations are in section 17.1/17.6 - `pytest tests` = **700 passed**, `pytest backend/tests/unit` = **493 passed**.

```powershell
# Repo
cd C:\Users\27363\Desktop\store
git log --oneline -5
git status --short

# Backend — expect 521 passed (section 17.6; full suite is 700)
cd backend
C:\Users\27363\Desktop\store\.venv\Scripts\python.exe -m pytest tests/unit tests/integration/identity -q
C:\Users\27363\Desktop\store\.venv\Scripts\python.exe -m ruff check app tests migrations

# Infrastructure — must be 5/5 healthy
cd ..
docker compose --env-file .env -f ops/docker-compose.yml ps

# Frontend
cd frontend ; npx vue-tsc --noEmit ; npx vitest run ; cd ..
```

Expected: tests green, lint clean, 5 containers healthy, typecheck exit 0.

If any of that is red, treat it as `RISK`-level: the previous agent may have been
interrupted mid-edit. `git stash` is the fastest way back to a known-good tree.

---

## 2. Environment (exact, because this cost real time to get right)

| Thing | Value | Why it matters |
|---|---|---|
| **Python** | `C:\Users\27363\Desktop\store\.venv\Scripts\python.exe` — **3.11.16**, uv-managed | The host default is Python **3.14.4** and it is **unusable**: it is a msys2/ucrt64 mingw build that uv refuses outright (`Unknown operating system: mingw_x86_64_ucrt_gnu`), and `onnxruntime` publishes no cp314 Windows wheels. **Always call the venv python by full path.** |
| Node / npm | 24.15.0 / 11.17.0 | |
| Docker | 29.7.2 + Compose v5.4.0 | Engine was initially stopped; start Docker Desktop if `docker info` fails. |
| uv | 0.12.18, installed via pip | Installs are 10-50x faster than pip. Use `python -m uv pip install --python <venv> ...` because `uv` is not on PATH. |
| Network | PyPI + npm registry reachable | GitHub push is **flaky** — expect `Connection was reset` and retry 2-4 times. It succeeds. |

### Ports — 3306 is already taken on this host

A pre-existing native `mysqld` (PID 7712) owns 3306. Everything else uses a
private block so nothing collides:

```
mysql 13306 · redis 16379 · qdrant 16333/16334 · minio 19000/19001
keycloak 18080 · api (dev) 18001 · vite (dev) 5173
```

### Credentials (dev only, all in `.env` which is gitignored)

```
MySQL:    user=nova  password=nova_dev_password  db=nova  root=rootpw
MinIO:    novaadmin / novasecret
Keycloak: admin / admin  (realm `nova`)
JWT:      dev-only-secret-not-for-production-use-0123456789abcdef
```

`.env.example` is the documented template. **`tests/unit/core/test_settings_contract.py`
fails the build if a settings field is undocumented or a documented key does
nothing** — if you add a setting, add it to `.env.example` too.

---

## 3. Architecture in one screen

```
backend/app/
  core/       config · context(contextvars) · logging · errors · redaction · middleware
  shared/     db/{base,types,session} · storage/ · redis_client · vector_store
  modules/    bounded contexts, each with enums/models/schemas/repository/service/router
  api/v1/     thin aggregation of module routers
  main.py     app factory + lifespan
```

**Layering is enforced, not suggested** (§14):

```
Controller | Agent | MCP          ← thin: parse, auth entry, call, map response
        ↓
Application Service / Workflow     ← business logic lives here
        ↓
Repository                         ← data access ONLY (§18)
        ↓
Infrastructure                     ← MySQL, Redis, Qdrant, S3, providers
```

Forbidden: Agent/MCP → Repository or DB · Controller → SQL or multi-table
transaction. An architecture test (`tests/architecture/`, gate FG-25) is planned
to enforce this mechanically.

**18 bounded contexts today in `modules/`:** `identity` (complete-ish),
`catalog` + `inventory` (models only). The rest are created per phase.

---

## 4. What is actually done, and the evidence

### Phase 0 — Audit + baseline lock ✅
`PROJECT_BASELINE.yaml` is **the contract**: 150+ requirements, 18 invariants
(INV-001..018), 26 final gates, 27 internal tools + 16 MCP tools, workflows,
dependency and protocol baselines. `docs/architecture/PHASE0_AUDIT.md` has the
narrative. Read `PROJECT_BASELINE.yaml` before making any design decision.

### Phase 1 — Skeleton / config / persistence / storage ✅
Settings (validated, production-hardened), `contextvars` trusted context,
purpose-aware redaction, stable business error codes + envelope, structured
logging, custom column types, object storage **port** with MinIO + in-memory
backends, Redis client, Alembic, `/health/live` + `/health/ready`.

### Phase 2 — Identity / auth / RBAC / DataScope ✅ **FG-13 PASSES**
8 tables, chain rotation with reuse detection, lockout, uniform login failure,
DataScope with an explicit `NONE`.

**Mandatory gate FG-13 = 28 integration tests passing.** Spec §116's five
required behaviours are all covered.

### Phase 3 — Catalog + inventory ✅ **FG-09 PASSES**
19 tables. `CHECK (available_qty >= 0)` / `CHECK (locked_qty >= 0)` and the two
delta-consistency constraints are confirmed present in `information_schema`.

`InventoryService.reserve/release/deduct/adjust` implement the §27 ordering rule,
and `tests/concurrency/test_inventory_oversell.py` proves the gate: 20 concurrent
callers against 1 unit, exactly 1 winner, real MySQL, **plus a negative control
that demonstrates the unlocked path really does oversell**.

The inventory API is live: 5 console endpoints + 1 customer endpoint, behind
`get_current_principal` and `require_permission`.

### Frontend — Phases 7 + part of 8 ✅ / 🔄
Full scaffold, JD-style (京东) redesign, Nova rename, 209 tests. 2/10 console
pages rebuilt. See `frontend/REDESIGN_STATUS.md`.

### Verification totals at handoff
```
pytest                153 passed  (119 unit + 28 identity integration + 6 concurrency)
ruff check            All checks passed
alembic autogenerate  true no-op (migrations have converged)
docker compose        5/5 healthy
frontend              vue-tsc 0 · vite build 0 · vitest 276 · eslint 0
FG-09                 PASS (artifact + 6/6 assertions, real MySQL)
```

---

## 4a. ⚠️ FRONTEND TYPE MIGRATION — read this before touching any view

The frontend scaffold was written **before** `API_CONTRACT.md` existed, against
invented shapes. When the contract landed, the frontend owner compared them and
found the mismatch was not "unconfirmed" but **wrong**. It correctly refused to
build eight more pages on top of types it now knew were incorrect.

**Consequence: several existing frontend types are wrong and must be migrated
before any view work continues.** Building on them means building to be rewritten.

### The concrete mismatches

| Area | Frontend invented | Frozen value |
|---|---|---|
| Order status field | `status` | **`order_status`** |
| Money | nested `snapshot.{items_amount,discount_amount,payable_amount}` | **flat**: `original_amount` / `promotion_discount_amount` / `coupon_discount_amount` / `shipping_amount` / `payable_amount` |
| Order line | `{product_title, cover_url, sku_specs, subtotal_amount}` | `{product_name, sku_name, image_url, unit_price, original_amount, payable_amount, allocated_discount_amount, after_sale_status}` |
| List vs detail | one `Order` with nested items | `OrderSummary` (no items) vs `OrderDetail` (+ `items[]` / `shipments[]`) |
| Receiver | unmasked | **already masked (§94)** — the client must not try to un-mask |
| Fulfillment | `Shipment{id: string}` | `Fulfillment{id: **number**, fulfillment_no, carrier (null until shipped), items[{id, order_item_id, sku_id, product_name, sku_name, quantity}]}` |
| Ship request | `{carrier, tracking_no, items[], idempotency_key}` | **exactly** `{carrier, tracking_no, item_quantities[]}` — no idempotency key |
| `carrier` | free text | **carrier code** (e.g. `"SF"`) |
| Inventory | `{on_hand, reserved, version}` | `{on_hand_qty, available_qty, locked_qty, safety_stock, sellable_qty, sku_no, product_name, sku_name, version}` |
| Adjust request | `{delta, reason, version, idempotency_key}` | `{warehouse_id, sku_id, version, delta_available, reason}` |
| Analytics | `{points:[{date,value}], money:boolean}` | `{metric, unit, period, series[{bucket,value}], summary{total,average,change_ratio}, dimensions[]}` |

### Already done (do not redo)

- `src/types/frozen-contract.ts` — a verbatim transcription of every frozen shape
  (`Paged<T>`, `PageMeta`, `emptyPage()`, `Fulfillment`, `ShipFulfillmentRequest`,
  `OrderSummary`/`OrderDetail`/`OrderItem`, `Inventory`, `AdjustmentPreview`,
  `CreateAdjustmentRequest`, `StaleVersionConflict`, the full analytics envelope
  and `AnalyticsUnit`). **Additive — it breaks nothing.**
- `src/domain/analytics/unit.ts` + 21 tests. The "a ratio rendered as ¥ is a
  silent lie" warning is now a test: `refund.rate = 0.12` must render `12%` and
  never `¥0.12`; `order_count = 137` never `¥1.37`. `minor_currency` is the only
  branch that yields money, and it returns the **raw integer** for `<PriceText>`
  rather than a formatted string, so no float ever touches an amount. Also guards
  against rendering a percentage-style `12` as `1200%`.
- Endpoint corrections applied: `/orders/admin` + `/orders/admin/{order_no}`
  (the `/orders/admin/orders` duplication is gone), `GET /fulfillments/admin`
  added, `ship` takes `number | string` because the id is numeric.

### Remaining migration, in this order (the order prevents rework)

**(a) Types + API layer.** Make `domain.ts`'s `Order` / `OrderItem` /
`OrderSnapshot` / `Shipment` into aliases of the frozen types, or delete them, so
each resource has **exactly one shape**. Update API module return types and
parameter names (`delta` → `delta_available`).

**(b) Availability modules + their 69 tests.** `orders/availability.ts` must read
`order_status`; `canShipOrder` must take a `Fulfillment` and decide "not yet
shipped" from `carrier === null`; `inventory/availability.ts` must use the
server's `sellable_qty` rather than recomputing it.

**(c) Views.** console Orders / Inventory / Analytics / Dashboard, and consumer
Orders / OrderDetail / Checkout / MockPay. **The Orders and Products console
pages already built are in scope too** — they were written against the old shapes.

### Also outstanding (markup tidiness, not defects)

- 73 `nx-card` + 17 `nx-pill` marker occurrences across 15 files. The aliases
  already render the dense visual, so these are consistency, not bugs.
- 7 console filter bars still use plain inputs rather than `.nx-filterbar`.
- `formatMoney` is fully retired (0 call sites, 0 imports, 69 `<PriceText>` uses),
  and 11 console tables now share `.nx-table`. Both complete.

---

## 5. Frozen decisions you must not silently change

Read these before inventing anything:

| Artifact | What it freezes |
|---|---|
| `PROJECT_BASELINE.yaml` | Requirements, invariants, gates, tool names, dependency pins |
| `docs/architecture/API_CONTRACT.md` | **Response shapes** — scalar encodings, list envelope, task endpoints, fulfillment/order/inventory/analytics shapes, error codes |
| `docs/architecture/ARCHITECTURE_INVARIANTS.md` | INV-001..018 and what enforces each |
| `docs/architecture/FINAL_GATE_INVENTORY.md` | The 26 gates and the 13 non-waivable conditions |
| `docs/architecture/IMPLEMENTATION_PLAN.md` | The 17-phase plan and per-phase exit criteria |
| `docs/adr/ADR-011-object-storage.md` | MinIO via quay.io, bucket layout, storage port |
| `docs/adr/ADR-013-mcp-sdk-compatibility-baseline.md` | MCP SDK 2.2.0 API surface, verified by introspection |

**Rules of engagement from the spec:** if reality conflicts with the design,
collect facts → check official docs → describe the conflict → propose the
**smallest** fix → record an ADR → continue. Never silently drop a requirement
(§150). Any one of the 13 non-waivable gates failing means `PROJECT STATUS = FAIL`
(§147).

---

## 6. Hard-won knowledge — do not re-learn these

Each of these cost a real failed command. They are recorded so you don't pay
again.

### Infrastructure
- **`docker.io/minio/minio` is NOT pullable** ("pull access denied"). Use
  `quay.io/minio/minio`. Verified.
- **MySQL 8.4 REMOVED `--default-authentication-plugin`.** Passing it aborts
  startup (`MY-000067`); a half-initialised volume then fails with `MY-013236`.
  Never add it.
- **MySQL errno 3823**: a column cannot both satisfy a `CHECK` constraint and sit
  in a foreign key whose referential action mutates it. That is why
  `MerchantScopedMixin` uses `ON DELETE RESTRICT` rather than `SET NULL`.
- **`information_schema.CHECK_CONSTRAINTS` has no `TABLE_NAME`** in MySQL 8.4 —
  join `TABLE_CONSTRAINTS` instead.
- The mysql CLI writes its password warning to **stderr**; it will look like a
  PowerShell error. Redirect with `2>$null` and read from a file.

### Toolchain
- **TypeScript 7 is unusable here.** `vue-tsc 3.3.11` resolves
  `typescript/lib/tsc`, and TS 7's `exports` map omits it →
  `ERR_PACKAGE_PATH_NOT_EXPORTED`. Verified against npm registry metadata:
  7.0.2 exports only `.`, `./unstable/*`, `./package.json`; 6.0.3 has no
  `exports` field at all. **Pinned 6.0.3.**
- **Vite 8 builds with rolldown.** Object-form `manualChunks` was removed → use
  `build.rolldownOptions.output.codeSplitting.groups`.
- **`baseUrl` is a hard error (TS5101)** from TS 6 onward. Paths are
  tsconfig-relative.
- The **Vite dev server bound only to `[::1]`** by default, so `127.0.0.1`
  refused — breaks Playwright and anything containerised. `server.host` is pinned.
- The backend mounts OpenAPI at **`/api/openapi.json`**, not the FastAPI default.

### MCP (Phase 12 will need this)
- SDK is **`mcp==2.2.0`** and **`FastMCP` no longer exists** — the class is
  `MCPServer` (`from mcp.server import MCPServer`). `import mcp.server.fastmcp`
  raises `ModuleNotFoundError`. Every tutorial you have read is for v1.
- `LATEST_PROTOCOL_VERSION == '2026-07-28'`, which **exactly matches** the
  spec §12 frozen baseline; `DEFAULT_NEGOTIATED_VERSION == '2025-03-26'` gives
  free legacy compatibility. **Do not hand-write protocol version handling.**
- OAuth Resource Server semantics and Origin/Host protection are **native**
  (`AuthSettings`, `TokenVerifier`, `TransportSecuritySettings`). No token
  passthrough needed.

### LangGraph (Phase 10 will need this)
- `StateGraph(state_schema, context_schema=...)` + `Runtime.context` is the
  native run-scoped trusted context (§70). It lives **outside** mutable state, so
  the model cannot forge it — that is exactly why it was chosen.
- **`interrupt()` re-runs the node from the top.** The SDK docstring says so
  verbatim. This is why §77 mandates splitting `CreatePendingAction` (which
  persists) from `AwaitApproval` (which must be side-effect free before the
  interrupt). Getting this wrong duplicates business side effects.
- `langgraph-checkpoint-redis 0.5.2` needs `langgraph-checkpoint>=4.1.1,<5`;
  local is `4.2.0` → compatible.

### MySQL arithmetic and Alembic blind spots (cost ~1 hour to untangle)

- **MySQL promotes a mixed signed/unsigned comparison to UNSIGNED.** A CHECK like
  `delta_available = after_available - before_available` looks obviously correct and
  is not: a decrement evaluates `0 - 1` in unsigned arithmetic, overflows, and the
  constraint **rejects a row that is arithmetically right**. FG-09 caught this on its
  first run. Fix: make every operand signed. Do not "fix" it with `CAST(...)` inside
  the constraint - see the next two points for why that is worse.
- **Alembic does NOT autogenerate CHECK-constraint changes on MySQL.** The migration
  produced for a constraint edit is *empty*, so the database silently keeps the old
  rule and you debug a constraint nobody updated. CHECK changes must be hand-written.
- **Alembic's `compare_type` does not distinguish `BIGINT` from `BIGINT UNSIGNED`**
  on MySQL either. The same trap, one layer down.
- Consequence: **after any schema change, verify the DDL actually landed** by reading
  `information_schema.CHECK_CONSTRAINTS` / `information_schema.COLUMNS`. Trusting
  "the migration ran with exit 0" is how both of the above hid for several rounds.
- **`ruff --fix` strips unused imports from an empty autogenerated migration.**
  If you later hand-write its body, `op`/`sa` are gone and you get a `NameError` at
  apply time. Re-add them.

- **MySQL REPEATABLE READ fixes a connection's snapshot at its first read.** A probe
  that reuses one connection across an out-of-band mutation can never observe the
  change, so an INV-014-style diff compares the snapshot against itself and passes as
  a **false green**. Use a fresh connection for every post-mutation read, and assert
  the mutation actually landed before asserting the historical row is unchanged - the
  two-way check is what exposed this during Phase 4's independent verification.

### Python / SQLAlchemy
- **`include_object` / `TypeDecorator` signatures are fixed by the frameworks.**
  Unused parameters get `# noqa: ARG001` **on the parameter line** — ruff reports
  per-argument, so a `def`-level noqa is both ineffective and itself flagged.
- **Alembic `compare_server_default` never converges**: MySQL normalises `now(3)`
  to `CURRENT_TIMESTAMP(3)`, so every autogenerate re-emitted 16 no-op ALTERs.
  Disabled deliberately; `compare_type` stays on.
- **Custom SQLAlchemy types need a `render_item` hook** in `migrations/env.py`,
  or autogenerate emits a fully-qualified name with no import and the migration
  raises `NameError` on first apply.
- **Relationships need explicit `foreign_keys` when two FKs point at the same
  table.** `user_roles` has both `user_id` and `granted_by` → without it,
  SQLAlchemy resolves against the *granting admin*, which is an authorization bug.
- **Always run `configure_mappers()` after touching models.** It caught two real
  relationship bugs. It is cheap and it fails loudly.
- `env_file=(".env",)` resolves against **CWD**, not the source tree. The config
  now resolves relative to `__file__` — a CWD-relative `.env` silently produced an
  *empty database password*, surfacing as a misleading "MySQL is not reachable".

### Frontend
- Money is **integer minor units** end to end. Conversion happens in exactly one
  place (`utils/money.ts::splitMoney`). `PriceText` **truncates rather than
  rounds** when `showDecimal:false`, because a checkout page must never display
  more than the server will charge.
- **Undefined CSS custom properties make a declaration invalid at computed-value
  time** — text silently falls back to an inherited colour. This is why the
  `--nx-*` aliases matter and had to be restored after they went missing.
- `--nx-*` / `nx-*` (720 occurrences) is **our own CSS namespace**, not a brand
  string. It was deliberately not renamed — 720 edits, zero user-visible benefit,
  real regression risk.

---

## 7. THE NEXT TASK — Phase 4: Cart + Pricing + Order

> **STATUS: DONE.** Phase 4 was completed and FG-10 passes (see section 16). This section is kept as the specification record; **the next task is Phase 5, described in section 17.**

FG-09 is done. The next mandatory gates are **FG-10 (workflow tests / order
snapshot correctness)** and, in Phase 5, FG-11 and FG-12.

### Why this is next
Inventory can now reserve stock, but nothing calls it in anger. Phase 4 is where the
inventory primitive becomes a real transaction, and where three invariants become
provable:

- **INV-006** `Order.payable_amount == SUM(OrderItem.payable_amount)`
- **INV-014** a historical order is immune to later product edits
- **INV-015** the same `Idempotency-Key` never produces a duplicate result

### What to build

1. **`PricingService` — the single price authority (spec §37).** Methods:
   `calculate_item_price`, `calculate_cart_price`, `apply_promotion`, `apply_coupon`,
   `calculate_shipping`, `build_price_snapshot`. Cart must not compute a price, the
   order must not recompute one, and the agent must not have a third opinion. One
   authority, one answer.

2. **`CreateOrder` accepts business inputs only (spec §38).** `sku_id`, `quantity`,
   `coupon_id`, `address_id`, `client_request_id`. **Never** `unit_price`,
   `discount_amount` or `payable_amount` from the client. The server recomputes
   every figure. This is the single most important rule in the phase: a client that
   can name its own price has bought the shop.

3. **Discount allocation (spec §41).** Order-level discounts are allocated pro-rata
   by each item's original amount, and **the remainder is absorbed by the last
   item**. The remainder rule is what makes INV-006 hold exactly rather than to
   within-a-cent; integer division always leaves something over and somebody has to
   take it. Assert `sum(items.payable_amount) == order.payable_amount` in the same
   transaction that writes them.

4. **Order + OrderItem with full trade snapshots (spec §30, §35).** `order_items`
   copies `product_name`, `sku_name`, image reference, `sku_snapshot`, `unit_price`
   and every discount field. It must never join live catalogue rows on the read
   path - that is precisely what INV-014 forbids, and a product rename silently
   rewriting last month's invoice is a legal problem, not just a bug.

5. **Order status machine (spec §31).** `PENDING_PAYMENT → PROCESSING` on payment;
   `PROCESSING → COMPLETED` on confirm-receipt; `CANCELLED`/`CLOSED` as specified.
   **Shipping never changes `order_status`** - it only moves `fulfillment_status`.
   Keeping these separate is the whole point of having four status fields.

6. **Idempotency (spec §48, §96).** `POST /orders` requires an `Idempotency-Key`
   header and a `client_request_id`. Replaying the same key with the same body must
   return the original result, not create a second order. Same key with a
   *different* body is a 409, not a silent reuse.

7. **Wire it up.** The order path calls `InventoryService.reserve` inside the same
   transaction, which is exactly what `tests/concurrency` just proved safe.

### Ordering rule to carry over
The §27 rule from Phase 3 generalises: **decide inside the lock, not before it.**
For orders that means the price and the stock check both belong inside the
transaction that writes the order.

### Carried into Phase 5 - a deletion with an owner

The frontend currently computes `refundable_amount` as `paid_amount - refunded_amount`
when the server field is absent. That bridge exists because the order module has not
landed yet, and without it every refund affordance silently disappears
(`undefined > 0` is `false`).

It is a **transition, not a second source of truth**, and it is deliberately noisy: it
warns in dev, is marked `@deprecated`, and tests pin both branches.

**Phase 5 is not done until that bridge and its test branch are deleted**, at the same
time `OrderDetail.refundable_amount` starts being returned. The backend implementer
owns the deletion. A TODO without a named owner is a permanent comment, and this one
would quietly make the client the authority on an INV-005 figure - which is exactly
what section 15 forbids.

### Gates to target
- **FG-10** workflow tests, including the order-snapshot test (materialise an order,
  mutate the product, assert the order is byte-for-byte unchanged)
- Then Phase 5 unlocks FG-11 (payment idempotency) and FG-12 (refund caps)

## 8. Remaining phases (from `IMPLEMENTATION_PLAN.md`)

| Phase | What | Gate |
|---|---|---|
| ~~3b~~ | ~~Inventory service + FG-09~~ ✅ **DONE** | **FG-09 PASS** |
| **4** | **Cart + Pricing + Order (PricingService is the single price authority)** | **FG-10** |
| 5 | Payment + Fulfillment + AfterSales + Refund | FG-11, FG-12 |
| 6 | Marketing + Analytics + Outbox | FG-08 |
| 7/8 | Frontend consumer ✅ / console 🔄 2 of 10 | FG-03, FG-20 |
| 9 | Tool Gateway (the narrow waist everything agent/MCP goes through) | FG-14, FG-25 |
| 10 | LangChain/LangGraph (Supervisor + 3 graphs; the §77 node split) | FG-15 |
| 11 | Knowledge / RAG (ingestion, hybrid retrieval, eval ≥50 cases) | FG-16, FG-17 |
| 12 | MCP server (`nova-commerce-mcp`) | FG-18 |
| 13 | AI Workspace / Governance UI | FG-20 |
| 14 | Security / tests / eval | FG-24, FG-26 |
| 15 | Docker / observability / deployment | FG-04, FG-23 |
| 16 | Docs / demo / final gate | FG-01, FG-22 |

**Still missing infrastructure code:** `scripts/run_evidence.ps1`,
`scripts/final_gate.py`, `ops/docker/Dockerfile.api`, `ops/keycloak/` realm
import, `tests/architecture/` (FG-25), `evals/`.

---

## 9. Open risks and known gaps

| # | Item | Severity | Note |
|---|---|---|---|
| 1 | **`.env` / `APP_ENV=dev` only** — no staging/prod validation run | MED | Prod hardening logic exists and is unit-tested, but never exercised end to end |
| 2 | **Frontend is ahead of backend.** 12 backend module APIs are absent. | HIGH | Frontend builds against the frozen contract and mocks at the API boundary. Every invented assumption is listed in `frontend/REDESIGN_STATUS.md` + task outputs. Integration is unproven. |
| 3 | **Keycloak is running but unused.** No realm import file yet. | MED | Phase 12 work. Spec §8 preferred Keycloak and nothing found so far requires replacing it. |
| 4 | Contract still unfrozen for: sort params, full `AgentRun`/`PendingAction` shapes, SSE payload internals, export formats | LOW | Deliberate — listed in `API_CONTRACT.md` §10 so absence isn't mistaken for permission |
| 5 | 720 `nx-*` CSS names not renamed | LOW | Deliberate, justified |
| 6 | Element (984 kB) and charts (1.08 MB) bundle chunks are large | LOW | Optimise once the console lands |
| 7 | **GitHub push is intermittent** | LOW | Retry loop; auth works |
| 8 | `FINAL_GATE.md` is still all ⬜ | — | Correct by construction: it is a computed index, not a claim |

---

## 10. How work has been done here (keep doing it)

The spec is unusually explicit about this, and it is the main reason the project
is in good shape:

- **§143 — never accumulate unverified files.** Each phase: implement → static
  check → migration → test → run → fix → evidence → docs. Phase 1 produced 9 real
  defects that the test suite caught; Phase 2 produced 4 (one of them a genuine
  security bug in the refresh grace window). Verifying late would have found none
  of them cheaply.
- **§149 — theoretical completion is forbidden.** "It should work" / "I didn't
  have time to run the tests" do not count.
- **§145 — `FINAL_GATE.md` is an index, never evidence.** A `PASS` written by hand
  proves nothing; the aggregator recomputes every verdict from `assertions[]`.
- **Verify subcontractors don't just report.** In this project the captain
  re-ran every claimed gate. That caught: a claimed "brand is Nexora" that was
  stated under a compliance heading without being checked (55 occurrences were
  still present), and an inaccurate commit message. Neither would have surfaced
  from the report alone.
- **When you find a spec gap, freeze it — don't let the consumer guess.**
  `API_CONTRACT.md` exists because the frontend correctly stopped and asked
  instead of inventing eight pages on top of an assumption.

---

## 11. First three commands for the next agent

```powershell
cd C:\Users\27363\Desktop\store
git log --oneline -3
& .\.venv\Scripts\python.exe -m pytest backend\tests\unit -q   # expect 493 passed (section 17.6; full suite is 700)
docker compose --env-file .env -f ops/docker-compose.yml ps     # expect 5/5 healthy
```

Then read `PROJECT_BASELINE.yaml` → `docs/architecture/API_CONTRACT.md` →
§7 of this document, and start on FG-09.


---

## 12. Handoff addendum — 13:00

**Frontend contract migration is complete** (commit `0150fd8`, 246 tests, four gates
green). All invented types were replaced with the frozen shapes, `order_status` is
used throughout, `canShipOrder` reads `carrier === null` on a `Fulfillment`, and
inventory reads the server's `sellable_qty` rather than recomputing it.

Three contract gaps it exposed are now closed (commit `ffbd14f`, contract §11):

| Field | Trigger |
|---|---|
| `OrderSummary.item_count`, `first_item_name` | Order lists could not name a product without an N+1 detail fetch per row. |
| `OrderDetail.cancel_reason` | A cancel-reason input decorated a value the server never sent or accepted. |
| `OrderDetail.refundable_amount` | Client-derived `paid - refunded` yields `undefined` on a missing field, and `undefined > 0` is `false` — silently disabling **every refund affordance** with no error thrown. |

That last one is worth carrying forward as a rule: **a money field that decides
whether a UI control renders must be server-owned.** It is INV-005 exposed to the
client, and §15 forbids the client from being its authority. Expect the same shape
of problem wherever a cap or a limit is rendered as a control.

**Still in flight at handoff:** `console/{AfterSales, Marketing, AiWorkspace,
Knowledge, System}View.vue` remain on pre-redesign markup (task t4). The AI
Workspace one must stay a narrow local view model — contract §10 says the full
`AgentRun`/`PendingAction` shapes are not frozen yet, and inventing a rich shape now
would be contradicted in Phase 10/13.

**Where the next agent starts:** `docs/architecture/API_CONTRACT.md`, then §7 above
(Phase 4 — Cart + Pricing + Order), then the §6 list of MySQL/Alembic traps so they
are not paid for twice.


---

## 13. Handoff addendum — 13:14 (Phase 8 complete)

All ten console views are converted to the dense pattern, each with a pure tested
availability module behind its row actions — 276 frontend tests, four gates green.
Commits `ec415bf`, `03aa108`; contract addenda at `24cde07`.

**Two flaws the conversion exposed, both the same shape as everything else this
session found — a frozen RULE with an unfrozen INTERFACE:**

1. Knowledge `reprocess`/`archive` were built against `/knowledge/admin/documents/...`
   instead of the frozen `/knowledge/documents/<built-in function id>/reprocess|archive`.
2. **Marketing had no promotion publish/unpublish at all**, despite §4 freezing both.
   Nothing in the tree could have caught this: the endpoints simply were not there.
   A missing endpoint is invisible to a type checker, a linter and a test suite.

Both are now in `endpoints.ts`. Promotion/coupon **creation** and **system role
management** were reported rather than invented, and are now frozen in contract §12.

The §12.3 lesson generalises and is worth carrying into Phase 4: **a page that cannot
be built from the frozen contract is evidence the contract is incomplete, not evidence
the page is unnecessary.** Five gaps were found this way; none was found by review.

Phase 4 notes: promotion lifecycle (DRAFT/ACTIVE/ENDED) and the `AgentRun` /
`PendingAction` shapes are still assumptions, marked in code and in
`frontend/REDESIGN_STATUS.md`. Freezing them is Phase 6 and Phase 10/13 work.

---

## 14. Handoff addendum — Phase 8 closed (frontend)

Ten console views done, 292 frontend tests, four gates green. The frontend owner also
wrote a standalone handoff into `frontend/REDESIGN_STATUS.md` — **a frontend successor
should read that first and this file second.**

### 14.1 A live duplication trap — fix it in ONE commit

`frontend/src/api/marketing.ts` still holds a **locally invented `Promotion`**
(`type` / `rule: Record<string, unknown>` / `start_at` / `end_at`) that now **coexists**
with the real frozen `Promotion` from contract section 13.2. `MarketingView.vue` reads
the old field names, so it looks up `type` where the server sends `promotion_type`.

**This is the exact defect this codebase already cleared once for `Order`** — two shapes
for one resource. Delete the invented type and migrate the view **in the same commit**:
leaving both in place for even one commit is how the second shape becomes load-bearing.

### 14.2 What the frontend validation actually covers

**Types and API-module mocks only. No end-to-end evidence exists anywhere in the
frontend.** The twelve `app/modules/*` backends are incomplete, so against a real server
every list currently renders Empty or Error. Stated plainly because "292 tests pass" and
"the UI works" are different claims, and only the first has been demonstrated.

Playwright still covers only the five t1 specs — none of AfterSales, Knowledge,
Marketing, System or AiWorkspace has been touched by an e2e test.

### 14.3 A cheap audit that found a frozen endpoint with no caller

`POST /agent/runs/<run_id>/cancel` was frozen by section 4 and **had no call site
anywhere**. It surfaced by auditing `src/api` exports against every view and store
reference. A missing caller produces no error, no warning and no test failure — exactly
like the missing marketing publish path before it.

**Run that audit again after each module lands.** It is the cheapest known way to find
"frozen but unreachable". Six more are currently unwired and listed in
`frontend/REDESIGN_STATUS.md`.

### 14.4 The four "looks right but is wrong" defects

None of these was catchable by a build:

1. Nested `snapshot.{items_amount,...}` where the contract sends flat fields.
2. `order.status` is `undefined`, so every `includes()` returns false and the console
   **silently hides every action** — no exception, no test failure.
3. `SalesTrend.money: boolean` made the unit of a number a guess.
4. A client-derived `refundable_amount` where `undefined > 0` is `false`, which would
   have silently disabled **every refund control**.

The common shape: **a wrong field name reads as `undefined`, and `undefined` fails quietly
in exactly the direction that hides capability.** That is why the contract now carries
shapes rather than only paths.

---

## 15. Pending rulings and a collaboration hazard

### 15.1 Two list endpoints must be frozen (ruling made, contract not yet updated)

`PUT /system/roles/{id}/permissions` and `POST /system/users/{user_id}/roles` are frozen,
but **there is no LIST route for roles or users anywhere in the contract**. `Role.id` is
the *input* to that write, so with no way to enumerate roles the client cannot discover an
id to update.

**This is the same gap section 5.2 closed for fulfillments** (`ship` needs a fulfillment
id, so `GET /fulfillments/admin` was frozen). The frontend correctly stopped rather than
guessing, and the ruling is:

Freeze `GET /system/roles` and `GET /system/users`, subject to three conditions:

1. Both use the paged envelope of section 3, never a bare array.
2. `GET /system/roles` returns only roles visible to the current merchant -
   `merchant_id = current OR merchant_id IS NULL` (system roles are global). **The filter
   is a server-side authorization decision, not a client-side convenience.**
3. Role editing is a **review flow, never a toggle grid.** The four server obligations in
   section 13.4 (wholesale replacement, refuse a change lowering an `is_write` tool's
   `risk_level`, refuse `is_grantable = false`, audit the before/after set) mean the UI must
   show what is about to change before confirming. The coupon/promotion preview-then-confirm
   pattern is the template.

**Who writes it:** whoever holds `docs/` in the active session. The captain at the time of
this handoff could not, because a second agent was editing that file (see 15.3).

### 15.2 A corrected lesson - recorded because the wrong version is worse than none

A search returned zero hits on `HANDOFF.md` and the conclusion drawn was "the text is not
there". It was there (five places). The first explanation offered was a relative-path
problem, which was **wrong**:

```
-Pattern 'refundable_amount|bridge' -SimpleMatch   -> 0 hits   <- the bad command
-Pattern 'refundable_amount|bridge'                -> 5 hits
-Pattern 'refundable_amount' -SimpleMatch          -> 3 hits
Resolve-Path HANDOFF.md -> C:\...\store\HANDOFF.md          <- path was fine
```

The real cause is that **`-SimpleMatch` disables regex, so `|` is matched as a literal
pipe** - the pattern became the single literal string `refundable_amount|bridge`, which
appears nowhere.

This is recorded at length because **the plausible-but-wrong explanation pointed at a fix
that would not have prevented a recurrence**: an absolute path does not rescue a pattern
being treated as a literal. The two durable lessons are:

1. `-SimpleMatch` turns `|` into a literal. Know which mode your search is in.
2. **Never report a search result without the command that produced it.** Zero hits looks
   exactly like genuinely absent, and acting on a false zero means re-writing text that
   already exists.

The first attempt at this diagnosis was made by the captain and corrected by a teammate -
which is the system working, not failing.

### 15.3 Collaboration hazard - one working tree, two agents

A second agent began Phase 4 in the same working directory while the first was still
committing. A `git add -A` then swept that agent's in-flight work into an unrelated commit
(`f637336`), producing a commit whose message described one thing and whose contents were
another. It was corrected in `49a8b27`.

**Never take a whole-tree snapshot while another agent is editing.** Stage the paths you
own, one at a time. If two agents must work in parallel, give them separate worktrees or
separate clones - not one directory.

### 15.4 Frontend state at this handoff

- **295 tests, four gates green**, tree clean at `db7fed9`.
- Promotion creation is **done**: per-type discriminated `rule_config`, rates in basis
  points, explicit `scope`, `preview_token` required in the type so a single-submit create
  **cannot be written**, conflicts rendered rather than treated as an error.
- The duplicate local `Promotion` in `api/marketing.ts` is **deleted**; the compiler named
  the three view renames, which is the payoff for migrating types before views.
- Role editor: **blocked** on 15.1, deliberately unbuilt rather than guessed.
- `knowledgeAdminApi.createBase` is **unwired** - the Knowledge page can list bases, upload,
  reprocess and archive but cannot create one, so a fresh deployment has nothing to operate
  on and no UI way to fix that. Smallest remaining visible hole.
- **No e2e coverage and no end-to-end evidence.** Everything is verified at the type and
  API-module-mock level only, because the twelve module backends are incomplete.

---

## 16. Handoff addendum — Phase 4 complete (Cart + Pricing + Order)

**Phase 4 is complete and FG-10 passes on real MySQL.** Backend tree at `84f867d`;
`pytest tests` = **700 passed**, `ruff check app tests migrations` clean,
`alembic check` = no new operations, docker 5/5 healthy. Commits: `1a54156`
(pricing), `ba03102` (persistence + migration), `426d9d5` (workflow/API/tests),
through `84f867d` (FG-10 evidence).

### Delivered

* **`PricingService`** is the single price authority (spec §37): the six frozen
  methods, pure and DB-free, 269 unit tests, 100% statement coverage. Pro-rata
  allocation hands the remainder out backwards over positive weights, so a line's
  `payable_amount` can never go negative; `build_price_snapshot` asserts INV-006 and
  refuses a non-zero shipping charge.
* **Four tables** — `orders`, `order_items`, `order_status_logs`,
  `idempotency_records`; migration `a7c4e91b2d63`; 15 named CHECKs verified in
  `information_schema`; all money columns signed BIGINT.
* **`CreateOrderWorkflow`** — business inputs only (spec §38), one transaction,
  stock reserved under the row lock (spec §27), the order inserted *before* the
  reservation so every `ORDER_LOCK` movement carries `reference_id=order.id`
  (INV-007), merchant-scoped warehouse resolution, idempotency record committed with
  the order, INV-006 asserted in-transaction.
* **Frozen API paths** (§96/§14): `POST /orders/preview`, `POST /orders`,
  `GET /orders`, `GET /orders/{order_no}`, cancel, confirm-receipt,
  `GET /orders/admin`, `GET /orders/admin/{order_no}`. Consumer reads are own-only
  (50003, never 403); admin reads are merchant-scoped and permission-checked.
* **FG-10 evidence**: `artifacts/evidence/integration/fg10_workflow.xml` (frozen
  proof path) + `.json` — 170 assertions, all PASSED, exit 0, verdict PASS.
* Docs: `ORDER_WORKFLOW.md` (lifecycle/invariants), `PHASE4_DESIGN.md` (freeze),
  `PHASE7_ORDER_INTEGRATION_NOTES.md` (frontend deltas).

### Decisions carried out of Phase 4 (do not re-open silently)

1. **The cart is not a server resource.** The client sends its selection to
   `POST /orders/preview`; there is no `/cart` API (contract §14.1).
   `frontend/src/api/cart.ts` is dead code — Phase 7 deletes it.
2. **`shipping_amount` is 0 in V1.** A paid policy needs an allocation decision
   first (there is no per-item field for it, and it would break INV-006).
3. **`coupon_id` is accepted and refused with `90004`** until Phase 6 resolves
   coupons; promotions/coupons apply only through the internal `pricing_rules` seam.
4. **Shipping never changes `order_status`**; one state machine writes that field.
5. **Order create returns 200** on both create and replay.
6. `order_items.image_url` is persisted NULL with the durable `image_object_key`;
   read-time signing is a Phase 5/7 decision.
7. Per-order warehouse resolution is equivalent to per-line only because
   `load_priced_lines` refuses a multi-merchant cart; relaxing that guard requires
   per-line resolution in the same change.

### Fixed along the way (pre-existing defects)

* **App factory**: `build_api_router()` now returns one fresh aggregate per app
  instance. The old global router re-registered every module on each `create_app()`
  (duplicate OpenAPI operationIds, a route table that grew with the test count, HTTP
  tests erroring only in full-suite runs, and a duplicate `/api/v1/health` mount).
* **`tests/conftest.py`'s shared `client` fixture is broken** (httpx 0.28
  `ASGITransport` supports only async); Phase 4's order tests define their own
  `TestClient` fixture. Fix at source in Phase 5.
* **`alembic downgrade base` fails at the Phase 1 catalog revision** (errno 1553:
  `ix_sku_attribute_values_attribute` is dropped while the FK still needs it).
  Phase 4's own downgrade is clean. Minimal fix: drop the FK before the index in
  that revision's `downgrade()`.
* **`ruff format --check .` panics** inside ruff 0.16.8's annotation renderer on
  whole trees (pre-existing, not a gate; `ruff check` is clean).
* **Top-level `scripts/`** is outside `backend/pyproject.toml`'s `scripts/**`
  per-file-ignores (the config is anchored to `backend/`); run ruff from `backend/`
  for the project rule set, or extend that ignore entry.

### Frontend deltas for Phase 7

See `docs/architecture/PHASE7_ORDER_INTEGRATION_NOTES.md`. Headlines: four consumer
order paths still use `/orders/orders/*`; `cartApi` is dead; the `api-contract.ts`
preview/create shapes predate contract §14; the `refundable_amount` bridge must be
deleted once a real HTTP response carries the field.

### Next

Phase 5 — Payment + Fulfillment + AfterSales + Refund (FG-11, FG-12). The order
status machine, the stock ledger and the idempotency table are ready for it; the
`PENDING_PAYMENT -> PROCESSING` edge and every `refunded_amount` writer belong there.

### Independent verification of Phase 4 (verifier task t4)

A second execution of the committed tests reproduced FG-10 independently: same
verdict, **170/170 PASS**, exit 0, JUnit testcase count agreeing with the stdout
parse (only the run timestamp differs from the captain's run). Three probes with
their own assertions, not the authors':

* **INV-014**: an order created through the real `OrderService`, the catalogue then
  mutated out-of-band by raw SQL (product name, SKU name, price) with the mutation
  confirmed to have landed before the order was re-read - every snapshot column and
  header field byte-identical, and the ORM view did not leak the new price.
* **INV-006**: raw SQL over rows the workflow actually committed -
  `payable_amount == SUM(item.payable_amount)` **and**
  `payable_amount == SUM(original_amount - allocated_discount_amount)` per order,
  with a non-empty result set so the assertion cannot pass vacuously.
* **Idempotency race**: 8 threads released together on one `Idempotency-Key` -
  exactly one `orders` row, one `idempotency_records` row, and all eight callers
  returned the **same** `order_no` (one creator, seven replays). The unique index,
  not application logic, is what serialised them. The companion distinct-key control is a committed
  test (`test_concurrent_creates_with_different_keys_make_one_order_each`) and
  passes inside the same gate: 8 different keys produce 8 orders and 8
  reservations, so the uniqueness above is the key guard doing the work rather
  than a global mutex. (An earlier ad-hoc version of that control was
  inconclusive; the committed test supersedes it.)

The shared dev database was left clean afterwards: 0 rows in all four Phase 4
tables and no leftover scratch schemas.

---

## 17. Session handoff - Phase 4 closed, start Phase 5 here

> Written 2026-09-23 for the next conversation. **Read this section first**, then
> section 6 (MySQL/Alembic traps) and the Phase 5 entry in section 8.

### 17.1 Repository state

* `HEAD = 405c88b`, **pushed**; `origin/main == local`; working tree clean.
* `pytest tests` = **700 passed**; `ruff check app tests migrations` clean;
  `alembic check` = no new operations; `docker compose ps` = 5/5 healthy.
* **FG-10 PASS**: `artifacts/evidence/integration/fg10_workflow.xml` (frozen proof
  path) + `.json` - 170/170 assertions, exit 0, regenerated with the emitter's
  default directory target. Independent verification is recorded at the end of
  section 16.
* Backend modules present: `identity`, `catalog`, `inventory`, `pricing`, `order`.

### 17.2 What Phase 4 delivered (do not redo)

`PricingService` (single price authority, 269 unit tests), the four tables
(`orders`, `order_items`, `order_status_logs`, `idempotency_records`, migration
`a7c4e91b2d63`), `CreateOrderWorkflow`, the eight frozen order endpoints, the status
machine, and `order/{schemas,serializers,state_machine,service,workflow}.py`.
Read `docs/architecture/ORDER_WORKFLOW.md` and section 16 for the decisions that
must not be re-opened (cart is client-side; shipping is 0 in V1; coupon_id is
refused with 90004 until Phase 6; shipping never changes `order_status`).

### 17.3 Team state (AgentTeams)

The team `phase4-orders` exists at `.agent-teams/phase4-orders` with three idle,
durable members: **pricing-author**, **order-data**, **verifier** (`order-flow` was
removed after its work was committed). A new captain may create its own team; if the
harness exposes the existing members they can be reused. Hard rule from this phase
(section 15.3): **one file, one writer; commit path-scoped, never `git add -A`** -
another agent session may still be editing `frontend/**` in this same working tree.

### 17.4 Phase 5 scope (Payment + Fulfillment + AfterSales + Refund)

Gates: **FG-11 (payment idempotency, mandatory)** and **FG-12 (refund invariants,
mandatory)**. Required reading: baseline `REQ-PAY-*`, `REQ-FUL-*`, `REQ-AFS-*`;
`ORDER_WORKFLOW.md` section 11; `API_CONTRACT.md` sections 4-6; HANDOFF section 6.

* `payments` + `payment_callbacks` with `UNIQUE(provider, provider_event_id)`;
  callback payload snapshot sensitive-field filtered; mock pay DEV/DEMO only and
  never settable by a normal JWT user (INV-008); payment success can only come from
  a verified callback.
* `PaymentSuccessWorkflow`: callback idempotency -> payment `SUCCESS` -> order
  `PENDING_PAYMENT -> PROCESSING` -> `ORDER_DEDUCT` movements -> fulfillment shell
  -> outbox row (the outbox table itself is Phase 6; keep/emit at the marked seam in
  `CreateOrderWorkflow`).
* `fulfillments` + `fulfillment_items`; `POST /fulfillments/{id}/ship` takes exactly
  `{carrier, tracking_no, item_quantities}`; **shipping never changes
  `order_status`**; multi-package per order.
* `after_sales` (claim) and `refunds` (money fact) are separate domains;
  `refunded_amount <= paid_amount`, item refund `<= item.payable_amount`; refunded
  and after-sale statuses are separate writers.
* FG-11 evidence wants concurrency on real MySQL (same provider event delivered N
  times -> exactly one payment confirm, one deduction, one fulfillment, no duplicate
  outbox effect); FG-12 wants the cap invariants proven at the database boundary.

### 17.5 Obligations carried into Phase 5

1. **Delete the frontend `refundable_amount` bridge** once `OrderDetail.refundable_amount`
   is confirmed over a real HTTP response (section 12, `ORDER_WORKFLOW.md` section 11).
   The backend field already exists; the deletion is the proof it is delivered.
2. Wire the **outbox seam** marked in `CreateOrderWorkflow` when Phase 6 lands.
3. **Single-merchant coupling**: per-order warehouse resolution is equivalent to
   per-line only because a multi-merchant cart is refused; relaxing that guard
   requires per-line resolution in the same change.
4. `tests/conftest.py`'s shared `client` fixture is broken (httpx 0.28
   `ASGITransport` is async-only); Phase 5 should fix it at source.
5. `alembic downgrade base` still fails at the **Phase 1** catalog revision (errno
   1553); Phase 4's own downgrade is canonical (up->down->up identical). Minimal fix
   recorded in section 16.
6. Pre-existing and not gating: `scripts/emit_fg09_evidence.py` lint;
   `ruff format --check .` panics inside ruff 0.16.8's renderer (use `ruff check`);
   top-level `scripts/` is outside `backend/pyproject.toml`'s per-file-ignores.

### 17.6 How to verify before claiming anything

```powershell
cd C:\Users\27363\Desktop\store
.\.venv\Scripts\python.exe -m pytest backend\tests\unit -q      # fast
cd backend
& ..\.venv\Scripts\python.exe -m pytest tests -q                            # 700 expected
& ..\.venv\Scripts\python.exe -m ruff check app tests migrations
& ..\.venv\Scripts\python.exe -m alembic check
cd .. ; docker compose --env-file .env -f ops/docker-compose.yml ps
```

Rule from the spec (sections 143/149): implement -> static check -> migration ->
test -> run -> fix -> evidence -> docs -> commit, and never write a verdict that
was not observed. The mandatory-gate pattern to copy is FG-09/FG-10: a test file on
real MySQL, a negative control, and a JSON/XML evidence artifact under
`artifacts/evidence/`.

### 17.7 First commands for the new conversation

```powershell
cd C:\Users\27363\Desktop\store
git log --oneline -5
git status --short
.\.venv\Scripts\python.exe -m pytest backend\tests\unit -q   # expect 493
docker compose --env-file .env -f ops/docker-compose.yml ps                 # expect 5/5
```

Then: `docs/architecture/API_CONTRACT.md`, `PROJECT_BASELINE.yaml` (Phase 5
requirements), `docs/architecture/ORDER_WORKFLOW.md`, and start Phase 5.

---

## 18. Session handoff - Phase 5 CLOSED, start Phase 6 here

> Written at the end of Phase 5 for the next conversation. **Read 18.1 (state),
> 18.2 (what is proven) and 18.5 (what is still open) first.** Section 18.4 is the
> measurement protocol; it is the part of this phase that cost the most to learn.

### 18.1 Repository state

* `HEAD = ecf361f`, **pushed**, `main...origin/main` clean (no ahead/behind), working
  tree clean.
* **85 commits** since Phase 4's `e6c0557`.
* `pytest tests` -> **1102 passed, 0 failed**.
* `ruff check --no-cache app tests migrations` -> clean. **Use `--no-cache`**: a cached
  run reported zero while three errors were live, and it cost two people a round trip.
* `alembic current` -> `3f1ae2c55c54` (head); `alembic check` -> no new operations.
* `docker compose --env-file .env -f ops/docker-compose.yml ps` -> 5/5 healthy.
* The remote is **SSH** (`git@github.com:Changxin-YR/Nova-Commerce.git`). HTTPS to
  github.com:443 is intermittent here; if a push hangs, check `ssh -T git@github.com`
  before troubleshooting anything else.

### 18.2 Both mandatory gates PASS

```
FG-11  artifacts/evidence/concurrency/fg11_payment_idempotency.json
       verdict=PASS  11 assertions  rev=0e2b0ec   relevant_paths_dirty=False
FG-12  artifacts/evidence/integration/fg12_refund_invariants.json
       verdict=PASS  13 assertions  rev=62cad40   relevant_paths_dirty=False
```

What they prove:

* **FG-11** - ten concurrent deliveries of ONE provider event on real MySQL produce
  exactly one payment confirmation, one `ORDER_DEDUCT` per line, one fulfillment shell,
  one `PENDING_PAYMENT -> PROCESSING` log, and one `PROCESSED` callback row. Plus the
  negative controls: a *distinct* event id against a settled payment, a CLOSED attempt,
  an unsigned delivery, and an amount mismatch.
* **FG-12** - four modules: the three refund caps against the **live** tables (a
  violating UPDATE inside a SAVEPOINT, errno 3819, the real constraint name, and a
  byte-identical re-read on a **separate** connection), the workflow caps that have no
  database mirror, the fulfillment quantity guard at full width and across two packages,
  and the two-thread race for the last refundable amount.

Both artifacts record a revision AND assert the paths they depend on were clean at it.
**Re-emit after the last code commit, never before** - see 18.4.

Also read `artifacts/evidence/integration/phase5_ddl_verification.md` sections 6 and 9:
its own "what this does not prove" section is more useful than its findings.

### 18.3 What Phase 5 delivered

| Domain | Modules | Endpoints |
|---|---|---|
| payment | `payments` + `payment_callbacks`, `PaymentSuccessWorkflow`, provider HMAC + payload filtering | 6 |
| fulfillment | `fulfillments` + `fulfillment_items`, shell, ship, multi-package | 3 |
| after-sales | claims + `RefundWorkflow` + both caps | 8 |
| data layer | six tables, one migration, DDL read-back, shared seed, residue tool | - |

Frozen surfaces: `API_CONTRACT.md` section 15 (payment object, callback contract,
fulfillment queue, after-sale/refund objects, the caps) and section 15.8
(`refundable_amount` on the order list rows). Internal contract: `PHASE5_DESIGN.md`.

Per-member handoffs, each written by its author for exactly this purpose:

* `docs/handoff/phase5-payment-workflow.md`
* `docs/handoff/phase5-data-layer.md`
* `docs/handoff/phase5-after-sales.md`
* `docs/handoff/phase5-contract-contradictions.md` - read this before implementing
  anything from `PROJECT_BASELINE.yaml`; it names two baseline/design divergences and,
  more usefully, states the boundary of what it checked.

### 18.4 MEASUREMENT PROTOCOL - read this before you trust any number

Full text in `PHASE5_DESIGN.md` section 13. It exists because this phase produced a
day's worth of false signals, every one of them honest:

1. **One test process at a time.** Four `pytest` processes on one `nova` schema produced
   failures on rows a fixture had *just committed*. The tell is that **the failing set
   moves between runs** and the tests pass in isolation - a deterministic bug does not
   behave that way.
2. **The migration can be downgraded mid-run.** One observed reading showed every Phase 5
   table MISSING with `alembic current` at Phase 4's revision, surfacing as
   `1146 Table 'nova.payments' doesn't exist`. That looks like a catastrophic code fault
   and is not. **Check `alembic current` before any re-run.**
3. **The suite is database-state dependent.** Same code: `1102 passed / 0 failed` on a
   clean DB, `10 failed / 3 errors` on a dirty one, because a failing test skips its
   teardown. Run `python scripts/residue.py` first and quote its line with the number -
   and note that `residue: 0` is only meaningful alongside the concurrency state.
4. **A defect report carries: the commit, the file hash, the path, and only then a
   traceback if the hashes match.** `Get-FileHash <file> -Algorithm MD5` distinguishes
   *different revision* from *different file*; four reports against one module were
   settled in one exchange by that comparison alone. If you cannot reproduce your own
   finding at HEAD, **say so and drop it** - a stale report costs the owner the same turn
   whether or not it carries a caveat.
5. **Reversal instructions need `git grep` for CONSUMERS, not just the symbol.** The
   symbol existing tells you it is implemented; the consumers tell you whether removal is
   a one-file edit or a coordinated multi-module one. Acting without that check is how
   five reversals on one column produced two invalidated gate emissions and a schema
   repair.
6. **A correlation that CONFIRMS your theory is when to be most suspicious.** Two people
   with the most evidence misattributed a real defect this phase - once to database
   residue, once to a stale cache - because the correlation pointed the same way as the
   hypothesis.
7. **A file that contributes zero tests is invisible.** Check `--collect-only` against the
   files on disk; one probe this phase was named so pytest would not collect it even by
   name, and its three cases "passed" while measuring nothing. And a guard never observed
   failing is a guard nobody has shown works: mutate it.

### 18.5 What is still open

**Known, measured, worth fixing early:**

1. **`purge_shop`'s delete order can raise `1451`.** `after_sale_items.order_item_id` ->
   `order_items.id` is `RESTRICT`, and an intermittent
   `(1451, ... fk_after_sale_items_order_item_id_order_items)` was measured when
   `tests/integration/commerce` and `tests/integration/aftersales` ran in one process
   (4 runs: 36 / 1 failed / 2 failed / 36). **I could not reproduce it in 3 further
   runs**, so treat it as intermittent rather than fixed: if a full-suite run shows 1-2
   errors that vanish on re-run, check this first.
   The fix is data-layer's file: audit `purge_shop` so every child of `order_items` and
   `orders` is deleted before its parent (`after_sale_items`, `refunds`,
   `fulfillment_items`, `order_status_logs`). Do **not** act on
   `phase5-data-layer.md` section 8 as written - it points at the FG-12 probes, and the
   verifier showed those are insulated (function-scoped shop, fresh merchant per test)
   and 10/10 stable.
2. **Per-process schema isolation.** Nothing isolates the database between concurrent
   runs; the freeze window is currently the only protection. A distinct
   `MYSQL_DATABASE` per process, or serialised runs, is the durable fix and the
   highest-value infrastructure change available.
3. **Two caps have no database mirror**: the per-line cumulative refund cap, and
   `after_sales.refunded_amount <= approved_amount`. A defect there raises no database
   error, which is why they get adversarial probes.

4. **`scripts/residue.py` checks only one direction.** It detects
   *child-with-missing-parent* (`orphaned_callbacks`) but has no
   *parent-with-missing-child* check - a balance whose ledger rows went, or an
   `inventories` row that `verify_ledger` (INV-007) cannot explain. The tool carries the
   same asymmetry as the hypothesis it was built from, which is why the omission is
   worth naming: the failing direction is usually the one the author did not think of.
   The query is written and verified:
   ```sql
   SELECT COUNT(*) FROM inventories i
   WHERE NOT EXISTS (SELECT 1 FROM inventory_movements m
                     WHERE m.warehouse_id = i.warehouse_id AND m.sku_id = i.sku_id)
   ```
   Run it in **both** directions after any purge change. Found by the fulfillment
   author while auditing the teardown for exactly this class, and left as a finding
   rather than a request because the tool is data-layer's file.

**Documentation:**

5. **`REQ-PAY-001` wording.** The baseline says `payments: payment_no UNIQUE`; the schema
   scopes it to `(merchant_id, payment_no)`, which is correct for a multi-merchant
   deployment. Correct the baseline wording rather than adding a redundant global unique.
6. **`c9eaac3` is a 4-file partial commit** from a concurrent `git reset`; `a785744` is
   the real one. A history wart, pushed, not squashed - rewriting history under four
   writers was judged the bigger risk.

**Limits of the evidence, stated by its own authors:**

6. FG-12's concurrency probe has passed 10 consecutive runs; FG-11's gate 6/6. Neither
   has been run hundreds of times or with parallel processes, so flakiness is *bounded,
   not eliminated*.
7. No real (non-MOCK) payment provider is exercised; `API_CONTRACT.md` section 15.7
   leaves that unfrozen.
8. The DDL evidence's negative controls use twins for the *clause*; the live-table
   enforcement claim comes from the verifier's module. Both are needed, and the artifact
   labels which is which.

### 18.6 Decisions that must not be re-opened silently

Each of these cost real turns to settle. If you disagree with one, say so explicitly and
say what changes - do not "tidy" it.

* **`fulfillment_items.sku_id` EXISTS** and both read paths read the column.
  `REQ-FUL-002` enumerates **required** columns, not an exhaustive set - it also omits
  `id`, `created_at`, `updated_at`, `product_name` and `sku_name`, which the table has.
  Two paths deriving it while the column existed was the one genuinely bad state; one
  rule in both modules is the settled one.
* **The baseline outranks `PHASE5_DESIGN.md`.** The design is the captain's working
  document; the baseline is the machine-readable contract.
* **A refund does NOT write `orders.fulfillment_status`.** A returned parcel was
  delivered *earlier*, by a delivery fact; the refund writing `DELIVERED` would be money
  movement asserting a logistics fact (section 31's axis separation).
* **An approved after-sale claim does NOT move `orders.after_sale_status`** - only
  `RefundWorkflow` does, because that axis is money.
* **Shipping never moves `order_status`.** "Can this be shipped?" reads
  `fulfillment_status`, which is a rollup over all packages.
* **Per-line refund shares are weighted by each line's REMAINING capacity**, and the
  split must partition the refund (`sum(shares) == amount`). The design text said
  otherwise and was wrong.
* **`REFUND_EXCEEDS_PAID_AMOUNT` (80004) is unreachable through the workflow** -
  `sum(line.payable) == payment.paid_amount` makes cap 2 imply it. Do not write a gate
  assertion expecting 80004 from that path; assert the identity that makes it
  unreachable, plus the inclusive boundary (`refunded == paid` is ACCEPTED).
* **`CHECK` constraints are hand-written and Alembic cannot see their changes.** After
  any schema edit, read `information_schema.CHECK_CONSTRAINTS` (join
  `TABLE_CONSTRAINTS` - `CHECK_CONSTRAINTS` has no `TABLE_NAME` on MySQL 8.4) rather than
  trusting a migration exit code.
* **`ALTER TABLE` implicitly commits** and a savepoint cannot roll it back. A DDL
  meta-control belongs on a table the test owns outright.
* **Mock payment surfaces are `{dev, test}` only.** `AppEnv` has no `demo` member.

### 18.7a Deferred prose refinements (small, non-blocking, captured before the session ended)

These were agreed by their authors but only recorded in messages, so they are written
here rather than lost with the conversation. Each is a documentation edit to a
per-member handoff; none changes behaviour.

1. **`docs/handoff/phase5-payment-workflow.md`** - add the second half of the
   measurement rule: *a scoped query is evidence about the **scope**, not about the
   table.* "My fixtures left nothing" and "the table is clean" are different claims, and
   a flat reading only means something against a state in which it could have differed
   (a `0` beside a `0` proves nothing; the same scope reading `0` while the table held
   `109` does).
2. **Same file** - add: *assert the **property** ("my rows are gone"), never the
   **implementation** ("my purge ran")*, because a purge that runs and deletes nothing
   passes an implementation assertion and fails a property assertion. That was the exact
   bug in the seed's `LIKE '<marker>%'` against `evt-<marker>-...`.
3. **Same file** - tighten the `providers.py` docstring. It claims the marker/`key` count
   is pinned so *additions* are visible, but the mutation check showed the test also
   catches a **removal** (`marker list -1` fails). The claim is accurate but understated,
   and an understated guard is one somebody later deletes as weaker than it is.
4. **`docs/handoff/phase5-after-sales.md`** (`f4a2ade`, later corrected at `ecf361f`) -
   note the forward obligation: if a future edit makes the refund or claim path write
   `payment_callbacks`, the teardown obligation arrives with it. Their fixtures write no
   callbacks today, which is why nothing leaks; that is a current fact, not a property,
   and a correctness argument that holds only because of a fact elsewhere is a latent
   bug.
5. **`scripts/residue.py`** - see 18.5 item 4: add the parent-with-missing-child check.
   The query is written out there.

Two habits worth carrying, both offered by their authors because each could see the
other's blind spot and not their own:

* **A mutation proves a test is capable of failing; a collection check only proves it
  ran.** A suite can be complete and still be insensitive.
* **Cross-read prose, not just tests.** Four defects this phase were statements about
  one module living inside another module's file - an author is systematically blind to
  the category they are standing inside, and a reviewer reading the text *to use the
  interface* is not.

### 18.8 First commands for the new conversation

```powershell
cd C:\Users\27363\Desktop\store
git log --oneline -6
git status -sb                                   # confirm in sync with origin
ssh -T git@github.com                            # SSH is the working remote
cd backend
& ..\.venv\Scripts\python.exe -m alembic current          # is the schema at head?
& ..\.venv\Scripts\python.exe ..\scripts\residue.py       # record this line with any reading
& ..\.venv\Scripts\python.exe -m pytest tests -q          # expect ~1102 passed
& ..\.venv\Scripts\python.exe -m ruff check --no-cache app tests migrations
& ..\.venv\Scripts\python.exe -m alembic check
cd .. ; docker compose --env-file .env -f ops/docker-compose.yml ps
```

Then read, in this order: `PROJECT_BASELINE.yaml` (the Phase 6 requirements),
`docs/architecture/API_CONTRACT.md` (section 15 for what Phase 5 froze),
`docs/architecture/PHASE5_DESIGN.md` section 13, this section, and the four
`docs/handoff/phase5-*.md` files. Phase 6 is **Marketing + Analytics + Outbox**, and its
first task is the outbox seam - marked, unmoved, and waiting in two places:
`CreateOrderWorkflow` step 9 and `PaymentSuccessWorkflow` step 10.
