# Final Gate Inventory

> Frozen by spec §146. Canonical machine-readable copy: `PROJECT_BASELINE.yaml` → `final_gates`.
>
> **These are the acceptance tests of the entire project.**
> A gate is `PASS` only when its evidence artifact exists, was produced by a
> real execution, and records `exit_code`. See `FINAL_GATE.md` for the index.

## The 26 gates

| Gate | Name | Evidence artifact | Mandatory |
|---|---|---|---|
| FG-01 | Repository State Audit | `artifacts/evidence/final/fg01_repo_state.json` | |
| FG-02 | Backend Static / Startup Check | `artifacts/evidence/build/fg02_backend_static.txt` | |
| FG-03 | Frontend Typecheck + Build | `artifacts/evidence/build/fg03_frontend_build.txt` | |
| FG-04 | Docker Build | `artifacts/evidence/docker/fg04_docker_build.txt` | |
| FG-05 | Alembic Migration | `artifacts/evidence/migration/fg05_alembic.txt` | |
| FG-06 | Seed Idempotency | `artifacts/evidence/migration/fg06_seed_idempotency.json` | |
| FG-07 | Unit Tests | `artifacts/evidence/unit/fg07_pytest_unit.xml` | |
| FG-08 | Integration Tests | `artifacts/evidence/integration/fg08_pytest_integration.xml` | |
| **FG-09** | **Inventory Concurrency** | `artifacts/evidence/concurrency/fg09_inventory_over_sell.json` | ✅ |
| FG-10 | Workflow Tests | `artifacts/evidence/integration/fg10_workflow.xml` | |
| **FG-11** | **Payment Idempotency** | `artifacts/evidence/concurrency/fg11_payment_idempotency.json` | ✅ |
| **FG-12** | **Refund Invariants** | `artifacts/evidence/integration/fg12_refund_invariants.json` | ✅ |
| **FG-13** | **Auth Session / Refresh Rotation** | `artifacts/evidence/auth/fg13_refresh_rotation.json` | ✅ |
| **FG-14** | **Agent Authorization** | `artifacts/evidence/agent/fg14_agent_authorization.json` | ✅ |
| **FG-15** | **LangGraph HITL Resume / No Duplicate Side Effect** | `artifacts/evidence/agent/fg15_hitl_resume.json` | ✅ |
| FG-16 | RAG Eval | `artifacts/evidence/rag/fg16_rag_eval.json` | |
| **FG-17** | **RAG Prompt Injection** | `artifacts/evidence/rag/fg17_rag_injection.json` | ✅ |
| **FG-18** | **MCP Authorization / Compatibility** | `artifacts/evidence/mcp/fg18_mcp.json` | ✅ |
| FG-19 | Object Storage | `artifacts/evidence/storage/fg19_storage.json` | |
| FG-20 | Vitest | `artifacts/evidence/unit/fg20_vitest.xml` | |
| FG-21 | Playwright | `artifacts/evidence/e2e/fg21_playwright.json` | |
| **FG-22** | **Flagship E2E** | `artifacts/evidence/agent/fg22_flagship_e2e.json` | ✅ |
| FG-23 | Health Checks | `artifacts/evidence/docker/fg23_health.json` | |
| FG-24 | Secret Scan | `artifacts/evidence/security/fg24_secret_scan.json` | |
| FG-25 | Architecture Constraint Check | `artifacts/evidence/architecture/fg25_architecture.json` | |
| **FG-26** | **Critical Security Demo** | `artifacts/evidence/security/fg26_critical_block.json` | ✅ |

## Mandatory gates — spec §147

The 13 conditions below are **non-waivable**. Any single failure sets
`PROJECT STATUS = FAIL`, regardless of everything else passing.

| # | Condition | Gate |
|---|---|---|
| 1 | Inventory concurrency never oversells | FG-09 |
| 2 | Payment callback is idempotent | FG-11 |
| 3 | Refund never exceeds amount actually paid | FG-12 |
| 4 | Order snapshot correctness | FG-10 |
| 5 | Agent cannot exceed user authority | FG-14 |
| 6 | Analytics agent cannot write | FG-14 |
| 7 | Unapproved PendingAction cannot execute | FG-15 |
| 8 | LangGraph resume does not repeat side effects | FG-15 |
| 9 | RAG injection cannot trigger a business write | FG-17 |
| 10 | MCP cannot bypass DataScope | FG-18 |
| 11 | Refresh token rotation is correct | FG-13 |
| 12 | Flagship agent E2E passes | FG-22 |
| 13 | Critical-risk action is blocked | FG-26 |

## Evidence contract — spec §144

Every gate must emit a machine-readable artifact containing at least:

```json
{
  "gate_id": "FG-09",
  "name": "Inventory Concurrency",
  "command": "...",
  "timestamp": "2026-09-22T23:00:00Z",
  "exit_code": 0,
  "duration_ms": 0,
  "stdout_ref": "raw/fg09.stdout.log",
  "stderr_ref": "raw/fg09.stderr.log",
  "assertions": [
    { "name": "exactly_one_order_succeeded", "expected": 1, "actual": 1, "pass": true },
    { "name": "available_qty_final", "expected": 0, "actual": 0, "pass": true },
    { "name": "never_negative", "expected": true, "actual": true, "pass": true }
  ],
  "verdict": "PASS"
}
```

Rules that make the evidence trustworthy:

1. **`verdict` alone proves nothing.** The aggregator recomputes the verdict
   from `assertions[]` and `exit_code`; a hand-written `"verdict": "PASS"` with
   failing assertions is reported as **FAIL**.
2. **`FINAL_GATE.md` is an index, not evidence.** Spec §145 is explicit. The
   aggregator ignores it.
3. **Concurrency and idempotency gates must name their real infrastructure.**
   A gate whose report does not prove it used a real MySQL server is rejected.
4. **No secrets in evidence.** Output is scanned before being written
   (spec §144); a hit fails FG-24 as well.
5. **Regenerable.** `scripts/run_evidence.ps1` reproduces every artifact from
   a clean checkout.
