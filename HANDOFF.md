# HANDOFF — Nexora/Nova Commerce V1

> **Read this file first.** It is written for an agent (or engineer) picking this
> project up with **zero prior context**. Everything needed to continue is here
> or is linked from here.
>
> Handoff written: 2026-09-23 08:40 · Repo `main` at commit `fb2d4c1`

---

## 0. TL;DR

**What this is:** An AI-native 3C e-commerce operations platform. Two halves that
must stay honest with each other: a real transaction system (inventory, orders,
payment, refund) whose invariants are proven by concurrency tests against real
MySQL, and a *controlled* agent layer that can read facts, analyse, and propose —
but can only write through a tool gateway with human approval.

**Where it stands:** Phases 0, 1, 2 complete and verified. Phase 3 is half done
(schema complete, service layer not started). The frontend is well ahead of the
backend. **The single most important next task is FG-09** (§7 of this document).

**The one-line rule that governs everything:** every claim must be backed by
command output that someone else can re-run. Not "it should work" — the spec
(§149) explicitly rejects theoretical completion.

---

## 1. Verify the current state in 5 minutes

```powershell
# Repo
cd C:\Users\27363\Desktop\store
git log --oneline -5
git status --short

# Backend — must be 147 passed
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

### Phase 3 (part 1) — Catalog + inventory **schema** ✅
10 more tables → 19 total. `CHECK (available_qty >= 0)` and
`CHECK (locked_qty >= 0)` **confirmed present in `information_schema`**, plus the
two delta-consistency constraints that make INV-007 a database fact.

**Phase 3 (part 2) is not started** — see §7.

### Frontend — Phases 7 + part of 8 ✅ / 🔄
Full scaffold, JD-style (京东) redesign, Nova rename, 209 tests. 2/10 console
pages rebuilt. See `frontend/REDESIGN_STATUS.md`.

### Verification totals at handoff
```
pytest                147 passed  (119 unit + 28 integration)
ruff check            All checks passed
alembic autogenerate  true no-op (migrations have converged)
docker compose        5/5 healthy
frontend              vue-tsc 0 · vite build 0 · vitest 209 · eslint 0
```

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

## 7. THE NEXT TASK — FG-09 (do this first)

> **This is a mandatory, non-waivable gate (§147 #1). Spec §113 forbids proving
> it against a mock database.**

### What must be true

Real MySQL. Stock = 1. **20 concurrent `CreateOrder` calls.** Required outcome:

- exactly **1** succeeds
- exactly **19** fail with insufficient stock
- final `available_qty == 0`
- `available_qty` and `locked_qty` are **never negative at any instant**
- every change is explained by an `inventory_movements` row (INV-007)

### What already exists (the database half is done)

```
inventories:          UNIQUE(warehouse_id, sku_id)
                      CHECK (available_qty >= 0)   ← verified in information_schema
                      CHECK (locked_qty >= 0)      ← verified in information_schema
                      version BIGINT               ← optimistic lock for background edits
inventory_movements:  append-only, before/after/delta columns
                      UNIQUE(idempotency_key)      ← makes INV-003 a DB guarantee
                      CHECK (delta_available = after_available - before_available)
                      CHECK (delta_locked    = after_locked    - before_locked)
```

The engine is already tuned for this in `app/shared/db/session.py`:
`innodb_lock_wait_timeout = 5` (MySQL's 50s default would make losers hang),
`READ-COMMITTED` isolation, UTC session, `pool_pre_ping`.

### What to build

1. **`app/modules/inventory/repository.py`**
   - `InventoryRepository` — plain reads
   - `lock_for_update(*, warehouse_id, sku_id) -> Inventory | None` using
     `select(Inventory).where(...).with_for_update()`
   - `append_movement(...)` — insert-only; never UPDATE a movement
   - **No business rules here** (§18). No stock checks, no policy.

2. **`app/modules/inventory/service.py`** — `InventoryService`
   - `reserve(sku_id, quantity, reference_type, reference_id, idempotency_key)`
     → **short transaction**: lock the row FOR UPDATE → re-read `available_qty`
     *inside the lock* → if `available < quantity` raise `InsufficientStockError`
     → decrement `available`, increment `locked`, bump `version`, append an
       `ORDER_LOCK` movement with `before_*`/`after_*` → commit.
     **The check must happen after acquiring the lock, never before** — a
     pre-lock check is a TOCTOU race and is exactly how oversell happens.
   - `release(...)` → reverse of reserve (`ORDER_RELEASE`)
   - `deduct(...)` → payment success moves `locked` → out (`ORDER_DEDUCT`)
   - `adjust(...)` → **optimistic** path using `version`, for low-contention
     background edits (§27). Returns 409 + current row on a stale version.
   - `build` movements via `InventoryMovement.build(...)` so deltas are *derived*,
     never passed in.

3. **`tests/concurrency/test_inventory_oversell.py`** (marker `concurrency`)
   - Seed one SKU, one `MAIN` warehouse, `available_qty = 1`
   - Fire 20 concurrent `CreateOrder`-equivalent reservers using a
     `ThreadPoolExecutor` (each thread needs **its own session** — a shared
     session is not thread-safe and would make the test meaningless)
   - Assert: `successes == 1`, `insufficient == 19`, final `available_qty == 0`,
     final `locked_qty == 1`, and sum of movements reconciles to the balance
   - **Also add a negative control**: temporarily assert that a naive
     read-then-write path *does* oversell, so the test proves the lock matters.
     A concurrency test that passes for the wrong reason is worse than none.

4. **Emit the evidence** to `artifacts/evidence/concurrency/fg09_inventory_over_sell.json`
   with the assertion-level shape documented in
   `docs/architecture/FINAL_GATE_INVENTORY.md` — `verdict` is recomputed from
   `assertions[]` + `exit_code` by `scripts/final_gate.py` (**not yet written**;
   writing it is part of Phase 14).

### Then, before Phase 4

- Wire the routers (`app/api/v1/router.py` already auto-discovers
  `app.modules.<ctx>.api.<submodule>.router` — just create the modules)
- Add catalog + inventory **schemas** (Pydantic request/response, explicit fields
  only — §110 mass-assignment guard)
- Seed script (idempotent, §126: Nexora→**Nova** Digital, 5 brands, 20-30
  products, 50-80 SKUs)

---

## 8. Remaining phases (from `IMPLEMENTATION_PLAN.md`)

| Phase | What | Gate |
|---|---|---|
| **3b** | **Inventory service + FG-09** | **FG-09** |
| 4 | Cart + Pricing + Order (PricingService is the single price authority) | FG-10 |
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
& .\.venv\Scripts\python.exe -m pytest backend\tests\unit -q   # expect 119 passed
docker compose --env-file .env -f ops/docker-compose.yml ps     # expect 5/5 healthy
```

Then read `PROJECT_BASELINE.yaml` → `docs/architecture/API_CONTRACT.md` →
§7 of this document, and start on FG-09.
