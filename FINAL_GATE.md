# FINAL GATE

This is an index of run artifacts. A gate passes only when its recorded tests
passed and its watched paths still match the tested revision.

**PROJECT STATUS: IN PROGRESS**

| Gate | Name | Mandatory | Artifact | Status | Evidence check |
|---|---|---|---|---|---|
| FG-01 | Repository State Audit |  | [artifacts/evidence/final/fg01_repo_state.json](artifacts/evidence/final/fg01_repo_state.json) | MISSING | No proof artifact |
| FG-02 | Backend Static / Startup Check |  | [artifacts/evidence/build/fg02_backend_static.txt](artifacts/evidence/build/fg02_backend_static.txt) | MISSING | No proof artifact |
| FG-03 | Frontend Typecheck + Build |  | [artifacts/evidence/build/fg03_frontend_build.txt](artifacts/evidence/build/fg03_frontend_build.txt) | PASS | 1 assertions at 7d7eca6 |
| FG-04 | Docker Build |  | [artifacts/evidence/docker/fg04_docker_build.txt](artifacts/evidence/docker/fg04_docker_build.txt) | MISSING | No proof artifact |
| FG-05 | Alembic Migration |  | [artifacts/evidence/migration/fg05_alembic.txt](artifacts/evidence/migration/fg05_alembic.txt) | MISSING | No proof artifact |
| FG-06 | Seed Idempotency |  | [artifacts/evidence/migration/fg06_seed_idempotency.json](artifacts/evidence/migration/fg06_seed_idempotency.json) | MISSING | No proof artifact |
| FG-07 | Unit Tests |  | [artifacts/evidence/unit/fg07_pytest_unit.xml](artifacts/evidence/unit/fg07_pytest_unit.xml) | PASS | 780 assertions at a9fb517 |
| FG-08 | Integration Tests |  | [artifacts/evidence/integration/fg08_pytest_integration.xml](artifacts/evidence/integration/fg08_pytest_integration.xml) | PASS | 341 assertions at a9fb517 |
| FG-09 | Inventory Concurrency | Yes | [artifacts/evidence/concurrency/fg09_inventory_over_sell.json](artifacts/evidence/concurrency/fg09_inventory_over_sell.json) | PASS | 6 assertions at a9fb517 |
| FG-10 | Workflow Tests | Yes | [artifacts/evidence/integration/fg10_workflow.xml](artifacts/evidence/integration/fg10_workflow.xml) | PASS | 170 assertions at a9fb517 |
| FG-11 | Payment Idempotency | Yes | [artifacts/evidence/concurrency/fg11_payment_idempotency.json](artifacts/evidence/concurrency/fg11_payment_idempotency.json) | PASS | 11 assertions at a9fb517 |
| FG-12 | Refund Invariants | Yes | [artifacts/evidence/integration/fg12_refund_invariants.json](artifacts/evidence/integration/fg12_refund_invariants.json) | PASS | 13 assertions at a9fb517 |
| FG-13 | Auth Session / Refresh Rotation | Yes | [artifacts/evidence/auth/fg13_refresh_rotation.json](artifacts/evidence/auth/fg13_refresh_rotation.json) | MISSING | No proof artifact |
| FG-14 | Agent Authorization | Yes | [artifacts/evidence/agent/fg14_agent_authorization.json](artifacts/evidence/agent/fg14_agent_authorization.json) | MISSING | No proof artifact |
| FG-15 | LangGraph HITL Resume / No Duplicate Side Effect | Yes | [artifacts/evidence/agent/fg15_hitl_resume.json](artifacts/evidence/agent/fg15_hitl_resume.json) | MISSING | No proof artifact |
| FG-16 | RAG Eval |  | [artifacts/evidence/rag/fg16_rag_eval.json](artifacts/evidence/rag/fg16_rag_eval.json) | MISSING | No proof artifact |
| FG-17 | RAG Prompt Injection | Yes | [artifacts/evidence/rag/fg17_rag_injection.json](artifacts/evidence/rag/fg17_rag_injection.json) | MISSING | No proof artifact |
| FG-18 | MCP Authorization / Compatibility | Yes | [artifacts/evidence/mcp/fg18_mcp.json](artifacts/evidence/mcp/fg18_mcp.json) | MISSING | No proof artifact |
| FG-19 | Object Storage |  | [artifacts/evidence/storage/fg19_storage.json](artifacts/evidence/storage/fg19_storage.json) | MISSING | No proof artifact |
| FG-20 | Vitest |  | [artifacts/evidence/unit/fg20_vitest.xml](artifacts/evidence/unit/fg20_vitest.xml) | PASS | 292 assertions at 7d7eca6 |
| FG-21 | Playwright |  | [artifacts/evidence/e2e/fg21_playwright.json](artifacts/evidence/e2e/fg21_playwright.json) | PASS | 5 assertions at 7d7eca6 |
| FG-22 | Flagship E2E | Yes | [artifacts/evidence/agent/fg22_flagship_e2e.json](artifacts/evidence/agent/fg22_flagship_e2e.json) | MISSING | No proof artifact |
| FG-23 | Health Checks |  | [artifacts/evidence/docker/fg23_health.json](artifacts/evidence/docker/fg23_health.json) | MISSING | No proof artifact |
| FG-24 | Secret Scan |  | [artifacts/evidence/security/fg24_secret_scan.json](artifacts/evidence/security/fg24_secret_scan.json) | MISSING | No proof artifact |
| FG-25 | Architecture Constraint Check |  | [artifacts/evidence/architecture/fg25_architecture.json](artifacts/evidence/architecture/fg25_architecture.json) | MISSING | No proof artifact |
| FG-26 | Critical Security Demo | Yes | [artifacts/evidence/security/fg26_critical_block.json](artifacts/evidence/security/fg26_critical_block.json) | MISSING | No proof artifact |
