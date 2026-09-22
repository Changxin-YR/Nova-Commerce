# Implementation Plan

> Frozen by spec §142. Canonical machine-readable copy: `PROJECT_BASELINE.yaml` → `implementation_plan`.
>
> ## The rule that governs every phase (spec §143)
>
> **Implement → Static Check → Migration (if schema changed) → Test → Run → Fix
> → Evidence → Update Docs → commit-ready** — and only then advance.
>
> Spec §143 is explicit about why: *"不要积累几十个未经运行验证的文件以后一次修"*
> (do not accumulate dozens of unverified files and fix them all at once).

---

## Phase map

| Phase | Name | Depends on | Exit gate(s) |
|---|---|---|---|
| 0 | Audit + Baseline Lock | — | ✅ **COMPLETE** |
| 1 | Skeleton + Infrastructure + MinIO | 0 | FG-02, FG-04, FG-23 |
| 2 | Identity + Auth Session + RBAC + DataScope | 1 | FG-13 |
| 3 | Catalog + SKU + Inventory | 2 | FG-09 |
| 4 | Cart + Pricing + Order | 3 | FG-10 |
| 5 | Payment + Fulfillment + AfterSales + Refund | 4 | FG-11, FG-12 |
| 6 | Marketing + Analytics + Outbox | 5 | FG-08 |
| 7 | Consumer Vue | 4, 6 | FG-03, FG-20 |
| 8 | Merchant Console | 7 | FG-20 |
| 9 | Tool Gateway | 2–6 | FG-14, FG-25 |
| 10 | LangChain / LangGraph | 9 | FG-15 |
| 11 | Knowledge / RAG | 9 | FG-16, FG-17 |
| 12 | MCP | 9 | FG-18 |
| 13 | AI Workspace / Governance UI | 10, 11, 12 | FG-20 |
| 14 | Security / Tests / Eval | 13 | FG-24, FG-26 |
| 15 | Docker / Observability / Deployment | 14 | FG-04, FG-23 |
| 16 | Documentation / Demo / Final Gate | 15 | FG-01, FG-22, all |

---

## Detailed phase plans

### Phase 1 — Skeleton + Infrastructure + MinIO

**Why first:** nothing in this project can be verified without real
infrastructure, and §113/§114 forbid mock databases for the mandatory gates.

Deliverables:
- `ops/docker-compose.yml` with mysql / redis / qdrant / minio / keycloak
  (observability behind an optional profile — §128 requires that observability
  is never a hard startup dependency)
- Port block from `PROJECT_BASELINE.yaml` (3306 is occupied on this host)
- MySQL 8.4 **without** the removed `--default-authentication-plugin` flag (G-03)
- `quay.io/minio/minio` (G-02 / ADR-011)
- Backend skeleton: `app/core/{config,logging,errors,context,security}`
- `app/shared/db/{base,session,types}` — SQLAlchemy 2.x declarative base with
  `BIGINT UNSIGNED` PK convention, `DATETIME(3)` convention, `utf8mb4`
- `app/shared/storage/` — S3/MinIO adapter with `object_key`/`checksum`/
  `content_type`/`size` (§20)
- Alembic bootstrap; **no** `create_all()` anywhere (§129)
- `/health/live` + `/health/ready` with MySQL=critical, Redis=important,
  Qdrant/LLM/reranker=degradable (§130)
- Structured JSON logging with `trace_id` propagation (§131)
- `.env.example` → verified against `Settings` by a test that fails if a
  `Settings` field has no documented key

Verification: `docker compose up`, `alembic upgrade head`, `/health/ready`
returns 200 with MySQL+Redis up; stopping Qdrant must **not** break
`/health/ready`.

### Phase 2 — Identity + Auth Session + RBAC + DataScope

- The 8 frozen tables (§21)
- `auth_sessions` stores **only** `refresh_token_hash` (§22) — asserted by a test
  that greps persisted rows for the plaintext token
- Refresh rotation with `rotated_from`; reuse of an old token revokes the family
  (§23, INV-016) → **FG-13**
- Argon2id password hashing
- RBAC: roles / permissions / `user_roles` / `role_permissions`
- **DataScope** (`SELF` | `MERCHANT` | `ALL`) as a first-class concept, because
  INV-010 and INV-012 both depend on it
- Mass-assignment guard: explicit request schemas only (§110)

### Phase 3 — Catalog + SKU + Inventory

- 7 catalog tables (§25) + 3 inventory tables (§26)
- `UNIQUE(warehouse_id, sku_id)`, `CHECK available_qty >= 0`, `CHECK locked_qty >= 0`
- Append-only `inventory_movements` with the full column set (§28)
- `SELECT ... FOR UPDATE` reservation path (§27)
- **FG-09**: 20 concurrent CreateOrder calls against 1 unit of stock on real
  MySQL → exactly 1 success, 19 failures, final `available = 0`, never negative

### Phase 4 — Cart + Pricing + Order

- `PricingService` as the single price authority (§37)
- CreateOrder accepts only business inputs; server recomputes all money (§38)
- Order + OrderItem snapshots (§30, §35) → INV-014
- Discount allocation pro-rata by original amount, remainder to the last item
  (§41) → INV-006
- Order status machine with the frozen vocabulary; paying moves
  `PENDING_PAYMENT → PROCESSING`, shipping changes **only**
  `fulfillment_status` (§31)
- `Idempotency-Key` + `client_request_id` (§48, §96)

### Phase 5 — Payment + Fulfillment + AfterSales + Refund

- `payments` + `payment_callbacks` with `UNIQUE(provider, provider_event_id)` (§42–43)
- Callback snapshot sensitive-field filtering (§43)
- **FG-11**: the same provider event delivered 10 times produces exactly one
  payment confirmation, one inventory deduction, one fulfillment and no
  duplicate outbox side effect — on real MySQL
- Multi-package fulfillment (§45)
- `after_sales` and `refunds` as separate domains (§46)
- **FG-12**: cumulative refunds never exceed paid; item refunds never exceed
  `item.payable_amount`

### Phase 6 — Marketing + Analytics + Outbox

- Promotions with the Strategy Pattern over `rule_config` (§39)
- Coupon lifecycle `UNUSED/LOCKED/USED/EXPIRED` with lock-on-order /
  use-on-payment / release-on-cancel (§40)
- Transactional outbox: business rows + outbox row commit together; worker
  publishes with retry and `next_retry_at` (§49)
- Reconciliation jobs for expired orders, outbox retry, expired coupons,
  expired pending actions, knowledge recovery (§50)

### Phase 7 — Consumer Vue

Vue 3 + TS + Vite + Element Plus. Routes per §100; states per §108; API client
per §106; Pinia stores limited to `auth/permission/cart/app/aiThread/notification`
(§105). Contract types generated from OpenAPI (§107, §141).

### Phase 8 — Merchant Console

Console route tree, RBAC-driven menu (§104 — UX only, never the real check).

### Phase 9 — Tool Gateway

The single narrow waist through which all agent and MCP traffic must pass:
`Resolve Trusted Context → Exists → Enabled → Agent Allowlist → Permission →
DataScope → Pydantic Input → Risk → HITL → Service/Workflow → Output Validation
→ Redaction → Audit` (§66). 27 internal tools (§67), and an architecture test
proving the forbidden tool names (§68) do not exist.

### Phase 10 — LangChain / LangGraph

Provider abstraction (§9–10) so no vendor SDK leaks into business modules.
`SupervisorGraph` + `ServiceGraph` + `OperationsGraph` + read-only
`AnalyticsGraph` (§72–75). Run-scoped trusted context via
`StateGraph(context_schema=)` and `Runtime.context` (§70). Budgets (§76).
**The §77 node split is a hard requirement**, guarded by an architecture test.

### Phase 11 — Knowledge / RAG

Ingestion pipeline (§52), state machine (§53), structure-aware chunking (§54),
full RAG metadata (§55), hybrid retrieval with RRF + rerank (§56), citation
builder that only accepts Evidence IDs (§58), visibility filtering (§59),
graceful degradation (§60). ≥50-case eval set with the five required metrics and
four pipeline comparisons (§120).

### Phase 12 — MCP

`nova-commerce-mcp` on `MCPServer` (mcp 2.2.0), stdio + Streamable HTTP at
`/mcp` (§86). OAuth Resource Server verification of issuer / signature /
expiry / audience-resource / scope with **no token passthrough** (§87).
Transport security settings (§88). Context derived only from verified identity;
tool parameters must not accept `user_id` / `merchant_id` / `is_admin` /
`permissions` (§89). 16 external tools (§91), no writes (§92),
`promotion.propose` creates only a PendingAction (§92).

### Phase 13 — AI Workspace / Governance UI

Assistant / Operations / Analytics / Pending Actions / Agent Runs (§101),
8 message block types, ChartSpec → ECharts with **no agent-emitted JavaScript**
(§102), Knowledge Center with retrieval debug + RAG evaluation (§103).

### Phase 14 — Security / Tests / Eval

Prompt-injection gate (§118), RAG-injection gate (§119), critical-risk demo
(§125/§126), secret scan (§134), the full §109 threat list.

### Phase 15 — Docker / Observability / Deployment

Multi-stage images, migration as a one-shot service (§129), OTel + Prometheus +
Grafana behind an optional profile (§128), full `trace_id` propagation (§131).

### Phase 16 — Documentation / Demo / Final Gate

All §137 docs, all §138 ADRs, §140 demo scripts, secret scan, and the final gate
sweep. `PROJECT STATUS` is computed from evidence, never asserted.

---

## Critical path and risk notes

**Critical path:** 1 → 2 → 3 → 4 → 5 → 6 → 9 → 10/11/12 → 13 → 14 → 16.

**Highest-risk items, and how they are contained:**

| Risk | Containment |
|---|---|
| Inventory oversell under concurrency (FG-09) | Build and prove the pessimistic-lock path *before* any of it is wrapped in an agent tool. Real MySQL only. |
| LangGraph interrupt re-executing the node (§77/INV-018) | The node split is enforced by an architecture test, not by discipline. Proven by a resume test that counts side effects. |
| Payment double-effect (FG-11) | Uniqueness lives in the database (`UNIQUE(provider, provider_event_id)`), not in application logic. |
| Discount allocation remainder (INV-006) | Deterministic remainder-to-last-item algorithm + a DB-level assertion after persist. |
| MCP auth semantics | Already de-risked in Phase 0: verified that mcp 2.2.0 natively expresses issuer/audience/scope. |
| Qdrant/reranker outage breaking commerce (§60/§130) | Qdrant is *degradable* in `/health/ready`; the commerce path holds no Qdrant dependency. |
| Frontend toolchain drift (TS 7 / Vite 8 / Pinia 4) | Validate `vue-tsc` + `vite build` with a trivial scaffold before writing features (Phase 7 entry check). |
