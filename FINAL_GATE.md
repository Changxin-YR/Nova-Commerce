# FINAL GATE

> ## ⚠️ This file is an echo, not evidence.
>
> Spec §145: *"FINAL_GATE.md 只是证据索引。它本身不是证据。"*
>
> A `PASS` written here proves nothing. Each row links to a machine-readable
> artifact under `artifacts/evidence/`. The aggregator
> (`scripts/final_gate.py`) **recomputes** every verdict from
> `assertions[]` + `exit_code` and **ignores any verdict string found here**.
>
> `PROJECT STATUS` is therefore a derived value, never an authored one.

---

## Status

| Field | Value |
|---|---|
| Project | Nova Commerce V1 |
| Spec version | `1.0.0-FINAL-DESIGN-FREEZE` |
| Baseline | `PROJECT_BASELINE.yaml` v1.0.0 |
| Base commit | `ea2410508950abfe86faac1cd470feda93325632` |
| Current phase | **Phase 0 complete — Phase 1 pending** |
| **PROJECT STATUS** | **IN PROGRESS** |

Legend: ✅ pass · ❌ fail · ⬜ not yet produced · ⏭️ out of scope for this phase

---

## Gate index

| Gate | Name | Mandatory | Artifact | Status |
|---|---|---|---|---|
| FG-01 | Repository State Audit | | `final/fg01_repo_state.json` | ⬜ |
| FG-02 | Backend Static / Startup Check | | `build/fg02_backend_static.json` | ⬜ |
| FG-03 | Frontend Typecheck + Build | | `build/fg03_frontend_build.json` | ⬜ |
| FG-04 | Docker Build | | `docker/fg04_docker_build.json` | ⬜ |
| FG-05 | Alembic Migration | | `migration/fg05_alembic.json` | ⬜ |
| FG-06 | Seed Idempotency | | `migration/fg06_seed_idempotency.json` | ⬜ |
| FG-07 | Unit Tests | | `unit/fg07_pytest_unit.json` | ⬜ |
| FG-08 | Integration Tests | | `integration/fg08_pytest_integration.json` | ⬜ |
| FG-09 | Inventory Concurrency | **✅** | `concurrency/fg09_inventory_over_sell.json` | ⬜ |
| FG-10 | Workflow Tests | | `integration/fg10_workflow.json` | ⬜ |
| FG-11 | Payment Idempotency | **✅** | `concurrency/fg11_payment_idempotency.json` | ⬜ |
| FG-12 | Refund Invariants | **✅** | `integration/fg12_refund_invariants.json` | ⬜ |
| FG-13 | Auth Session / Refresh Rotation | **✅** | `auth/fg13_refresh_rotation.json` | ⬜ |
| FG-14 | Agent Authorization | **✅** | `agent/fg14_agent_authorization.json` | ⬜ |
| FG-15 | LangGraph HITL Resume | **✅** | `agent/fg15_hitl_resume.json` | ⬜ |
| FG-16 | RAG Eval | | `rag/fg16_rag_eval.json` | ⬜ |
| FG-17 | RAG Prompt Injection | **✅** | `rag/fg17_rag_injection.json` | ⬜ |
| FG-18 | MCP Authorization / Compatibility | **✅** | `mcp/fg18_mcp.json` | ⬜ |
| FG-19 | Object Storage | | `storage/fg19_storage.json` | ⬜ |
| FG-20 | Vitest | | `unit/fg20_vitest.json` | ⬜ |
| FG-21 | Playwright | | `e2e/fg21_playwright.json` | ⬜ |
| FG-22 | Flagship E2E | **✅** | `agent/fg22_flagship_e2e.json` | ⬜ |
| FG-23 | Health Checks | | `docker/fg23_health.json` | ⬜ |
| FG-24 | Secret Scan | | `security/fg24_secret_scan.json` | ⬜ |
| FG-25 | Architecture Constraint Check | | `architecture/fg25_architecture.json` | ⬜ |
| FG-26 | Critical Security Demo | **✅** | `security/fg26_critical_block.json` | ⬜ |

---

## Non-waivable conditions (spec §147)

Any single ❌ below sets `PROJECT STATUS = FAIL` regardless of all other results.

| # | Condition | Gate | Status |
|---|---|---|---|
| 1 | Inventory concurrency never oversells | FG-09 | ⬜ |
| 2 | Payment callback is idempotent | FG-11 | ⬜ |
| 3 | Refund never exceeds amount actually paid | FG-12 | ⬜ |
| 4 | Order snapshot correctness | FG-10 | ⬜ |
| 5 | Agent cannot exceed user authority | FG-14 | ⬜ |
| 6 | Analytics agent cannot write | FG-14 | ⬜ |
| 7 | Unapproved PendingAction cannot execute | FG-15 | ⬜ |
| 8 | LangGraph resume does not repeat side effects | FG-15 | ⬜ |
| 9 | RAG injection cannot trigger a business write | FG-17 | ⬜ |
| 10 | MCP cannot bypass DataScope | FG-18 | ⬜ |
| 11 | Refresh token rotation is correct | FG-13 | ⬜ |
| 12 | Flagship agent E2E passes | FG-22 | ⬜ |
| 13 | Critical-risk action is blocked | FG-26 | ⬜ |

---

## Phase 0 evidence (already produced at baseline time)

Phase 0 is complete, and its own claims were verified by execution rather than
assertion. Raw results are recorded in `PROJECT_BASELINE.yaml` →
`infrastructure_smoke` and `docs/architecture/PHASE0_AUDIT.md`.

| Claim | Method | Result |
|---|---|---|
| Backend dependency set installs on Python 3.11.16 | `uv pip install -e ".[dev,parsers]"` | exit 0 |
| MySQL available and correct charset | `mysqladmin ping` + `SELECT VERSION()` in a real container | `mysqld is alive`, `8.4.11`, `utf8mb4_0900_ai_ci` |
| Redis available | `redis-cli ping` | `PONG` |
| Qdrant available | `GET /collections` | `{"status":"ok"}` |
| MinIO available | `GET /minio/health/live` | HTTP 200 |
| MCP SDK matches the frozen protocol baseline | package introspection | `LATEST_PROTOCOL_VERSION == '2026-07-28'` |

---

## How to regenerate this table

```bash
python scripts/final_gate.py --evidence-dir artifacts/evidence --out FINAL_GATE.md
```

The generator rewrites the tables above from the artifact directory. If an
artifact is missing, its row is `⬜`; if its recomputed verdict is `FAIL`, its
row is `❌` and `PROJECT STATUS` becomes `FAIL`.
