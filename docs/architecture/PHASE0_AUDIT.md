# Phase 0 — Repository, Git, Architecture & Dependency Audit

> Spec reference: §1 (开工前第一步), §150, §151, §153
> Status: **COMPLETE**
> Method: every fact below was produced by executing a command on the target
> machine or by introspecting the installed package — not from memory.

---

## 1. Repository Audit

| Item | Finding |
|---|---|
| Workspace | `C:\Users\27363\Desktop\store` |
| Initial state | **Empty directory.** No source files, no config, no history. |
| Nature of work | Greenfield. There is **no existing architecture to preserve or migrate.** |
| Git | Not initialised at audit time. |

**Consequence for the plan:** the "Existing Architecture Audit" reduces to
establishing the target architecture from the frozen spec, because there is no
legacy code whose decisions we must respect. The Gap Analysis therefore
enumerates requirement → implementation gaps, not migration steps.

---

## 2. Git Status Audit

| Item | Finding |
|---|---|
| Local repo | `git init -b main` executed during Phase 0 |
| Remote | `https://github.com/Changxin-YR/Nova-Commerce.git` |
| Remote reachability | Reachable (anonymous read succeeded) |
| Remote base commit | `ea2410508950abfe86faac1cd470feda93325632` — `Initial commit` |
| Remote tracked files | **1** (`README.md`, 51 bytes) |
| Remote branches | `main` only |
| Credential helper | **None configured** |

### 2.1 Naming discrepancy (raised, not silently resolved)

The remote README reads:

```
# Nova-Commerce
Nova Commerce|智能电商平台
```

The frozen spec §2 mandates the project name **Nexora Commerce**, and §86
freezes the MCP server identifier as **`nexora-commerce-mcp`**.

Two names now exist for one product. Silently picking one would violate
spec §150 ("do not silently modify the design").

**Resolution applied and recorded:**

- The frozen spec wins → the platform is implemented as **Nexora Commerce**.
- The *repository* is **not** renamed. A repository name is an address, not a
  product identifier, and renaming it would break the URL the product owner
  supplied.
- Status: `AWAITING_PRODUCT_OWNER_CONFIRMATION` (tracked as `RISK-001` in
  `PROJECT_BASELINE.yaml`). If the owner confirms "Nova Commerce", the change
  is a bounded string-level rename of branding constants; no architectural
  impact.

---

## 3. Existing Architecture Audit

No pre-existing architecture. The target architecture is fixed by spec §13–§18
and is adopted verbatim as the **Modular Monolith** contract:

```
Controller | Agent | MCP            (interface layer — thin)
            ↓
Application Service / Workflow      (use cases, orchestration, transactions)
            ↓
Repository                          (data access ONLY)
            ↓
Infrastructure                      (MySQL, Redis, Qdrant, S3, providers)
```

Bounded contexts (spec §13, 18 of them):
`Identity, Catalog, Inventory, Cart, Pricing, Order, Payment, Fulfillment,
AfterSales, Refund, Marketing, Analytics, Knowledge, Agent, MCP, Governance,
Audit` (+ `shared` as non-domain infrastructure).

Forbidden edges (§14, §17, §18) are enforced automatically rather than by
convention — see `tests/architecture/` and gate **FG-25**.

---

## 4. Dependency Audit (live registry probe, 2026-09-22)

### 4.1 Host runtime

| Tool | Version | Verdict |
|---|---|---|
| Python (host default) | 3.14.4 (msys2/ucrt64) | ❌ **rejected** |
| Python (uv-managed CPython) | 3.11.16 | ✅ **selected** |
| Node.js | 24.15.0 | ✅ |
| npm | 11.17.0 | ✅ |
| Docker client/server | 29.7.2 / 29.7.2 | ✅ (engine was stopped; started in Phase 0) |
| Docker Compose | v5.4.0 | ✅ |
| uv | 0.12.18 | ✅ (installed in Phase 0) |
| PyPI reachability | ✅ | |
| npm registry reachability | ✅ | |

**Why Python 3.14 was rejected** (spec §150 requires a factual reason, not
preference):

1. The host default is a **mingw/ucrt64** build. `uv` refuses it outright:
   `cause: Unknown operating system: mingw_x86_64_ucrt_gnu`. The toolchain
   cannot manage it.
2. `onnxruntime==1.30.0` (needed for the optional local reranker path,
   spec §57) declares `requires_python >=3.11` and publishes no `cp314`
   Windows wheels.
3. `numpy` latest requires `>=3.12`, forcing an older resolution for 3.11 —
   which is fine — but the reverse (running the AI stack on 3.14) sits at the
   sharp edge of the wheel matrix for SQLAlchemy/Celery/Qdrant/LangChain.

Selecting 3.11.16 is inside the spec (which says only "Python"), removes an
entire class of wheel risk, and is recorded in `PROJECT_BASELINE.yaml`.

### 4.2 Backend dependency resolution — VERIFIED BY INSTALLATION

`uv pip install -e ".[dev,parsers]"` against the 3.11.16 venv: **exit 0**.
Representative resolved versions:

```
fastapi 0.141.1        sqlalchemy 2.0.54      alembic 1.20.0
pydantic 2.13.5        pymysql 1.2.3          celery 5.6.3
redis 8.1.0            qdrant-client 1.19.1   minio 7.2.20
langgraph 1.2.12       langchain 1.4.2        langchain-core 1.6.4
langgraph-checkpoint 4.2.0                    mcp 2.2.0
```

### 4.3 Infrastructure image audit — VERIFIED BY PULL

| Image | Pull | Run | Notes |
|---|---|---|---|
| `mysql:8.4` | ✅ | ✅ 8.4.11 | |
| `redis:7.4-alpine` | ✅ | ✅ `PONG` | |
| `qdrant/qdrant:latest` | ✅ | ✅ `/collections` ok | |
| `quay.io/keycloak/keycloak:latest` | ✅ | deferred to Phase 1 | |
| `quay.io/minio/minio:latest` | ✅ | ✅ health ok | |
| ~~`minio/minio:latest`~~ | ❌ | — | Docker Hub denied |

---

## 5. Gap Analysis

Because the repository is empty, every requirement is a gap. The meaningful
analysis is therefore which gaps are **(a) already de-risked**, **(b) newly
discovered**, or **(c) still open**.

### 5.1 Newly discovered during Phase 0 (not stated in the spec)

| # | Gap | Impact | Resolution |
|---|---|---|---|
| G-01 | Host port **3306 is occupied** by an unrelated native `mysqld` (PID 7712) | Would break MySQL container startup | Private port block assigned (MySQL→13306, Redis→16379, Qdrant→16333/16334, MinIO→19000/19001, Keycloak→18080). Recorded in `PROJECT_BASELINE.yaml:ports`. |
| G-02 | `minio/minio` on Docker Hub returns **`pull access denied`** | Spec §20 object storage appears blocked | **Resolved:** `quay.io/minio/minio` pulls and runs. MinIO is retained; only the registry changes. → ADR-011. |
| G-03 | MySQL 8.4 **removed** `--default-authentication-plugin` | Naive compose file aborts with `MY-000067`; a half-initialised volume then fails with `MY-013236` | Omit the flag entirely; use `utf8mb4` server defaults and `utf8mb4_0900_ai_ci`. Pinned into the compose file contract. |
| G-04 | MCP Python SDK v2 **deleted the `fastmcp` module** (`FastMCP` → `MCPServer`) | Any MCP tutorial written for v1 fails immediately | Captured as a frozen API delta in ADR-013; implementation targets `mcp.server.MCPServer`. |
| G-05 | `langgraph-checkpoint-redis` version compatibility for §83 | Redis checkpointer is mandatory in deployment | **Compatible:** `0.5.2` requires `langgraph-checkpoint>=4.1.1,<5.0.0`; local is `4.2.0`. Satisfied. |
| G-06 | Docker engine was **not running** at start | All integration/concurrency gates impossible | Started during Phase 0; verified `Server 29.7.2`. |
| G-07 | Frontend toolchain is far ahead of common references (TS 7.x, Vite 8.x, Pinia 4.x, vue-router 5.x, ECharts 6.x, Vitest 5.x) | Copy-pasted scaffolding is likely wrong | Will be validated by an actual `vue-tsc --noEmit` + `vite build` before feature work in Phase 7. |

### 5.2 De-risked by Phase 0 (spec requirements now proven feasible)

| Requirement | Evidence |
|---|---|
| §87 MCP OAuth Resource Server semantics | `AuthSettings` natively exposes `issuer_url`, `resource_server_url`, `validate_token_resource`, `required_scopes`; `AccessToken` carries `resource` + `scopes`. No hand-written protocol, no token passthrough. |
| §88 MCP transport security | `mcp.server.transport_security.TransportSecuritySettings(enable_dns_rebinding_protection, allowed_hosts, allowed_origins)` + `TransportSecurityMiddleware` + `RequestBodyLimitMiddleware`. |
| §93 MCP structured output & read/write semantics | `Tool(input_schema, output_schema, annotations)`; `ToolAnnotations(read_only_hint, destructive_hint, idempotent_hint)`; `CallToolResult.structured_content`. |
| §12 2026 + 2025-era compatibility | SDK reports `LATEST_PROTOCOL_VERSION=2026-07-28` and `DEFAULT_NEGOTIATED_VERSION=2025-03-26` → native negotiation covers both. |
| §70 run-scoped trusted context | `StateGraph(state_schema, context_schema=...)` + `Runtime.context` — context lives outside mutable graph state, so the model cannot forge it. |
| §77 / INV-018 interrupt hazard | SDK docstring: *"The graph resumes from the start of the node, re-executing all logic."* Confirms the spec's rule; drives the CreatePendingAction / AwaitApproval node split. |
| §83 deployment checkpointer | `langgraph-checkpoint-redis 0.5.2` compatible with local `langgraph-checkpoint 4.2.0`. |

### 5.3 Open gaps (work, not blockers)

Every functional requirement `REQ-*` in `PROJECT_BASELINE.yaml` is unimplemented.
They are sequenced by the Phase plan; none is blocked by an unresolved
technical unknown.

---

## 6. Version-Sensitive Technology Baseline (spec §151)

Spec §151 requires querying current official sources *before* implementing
LangGraph, LangChain, MCP, Qdrant, FastAPI, SQLAlchemy, Alembic, Vue, Element
Plus.

**Method used, and why it is stronger than reading docs:** for the two highest
-risk technologies (which are also the two the spec explicitly warns about —
"don't copy stale MCP tutorials", "don't use removed LangGraph APIs") the
baseline was derived by **importing the installed package and printing its real
API surface**. Documentation describes intent; introspection describes what
will actually execute.

| Technology | Version | Verified how | Key finding |
|---|---|---|---|
| MCP | 2.2.0 | introspection + official SDK docs | protocol `2026-07-28`; `MCPServer` (not `FastMCP`); native OAuth RS + transport security |
| LangGraph | 1.2.12 | introspection + official source | `context_schema`, `Runtime.context`, `interrupt(value, *, response_schema=)`, `Command(resume=)`; interrupt re-runs the node from the top |
| LangChain | 1.4.2 / core 1.6.4 | installed metadata | v1 line |
| SQLAlchemy | 2.0.54 | installed metadata | 2.x style throughout |
| Alembic | 1.20.0 | installed metadata | |
| FastAPI | 0.141.1 | installed metadata | |
| Qdrant client | 1.19.1 | installed metadata | |
| Vue / Element Plus / Vite | 3.5.43 / 2.14.6 / 8.3.0 | live npm registry | see RISK-005 |

Full machine-readable record: `PROJECT_BASELINE.yaml` → `protocol_baselines`
and `dependency_versions`.

---

## 7. Phase 0 Exit Checklist (spec §153)

| Required output | Location | Status |
|---|---|---|
| Repository Audit | this document §1 | ✅ |
| Git Status Audit | this document §2 | ✅ |
| Existing Architecture Audit | this document §3 | ✅ |
| Dependency Audit | this document §4 | ✅ |
| Gap Analysis | this document §5 | ✅ |
| Frozen Requirement Inventory | `PROJECT_BASELINE.yaml` → `requirements` | ✅ |
| Architecture Invariant Inventory | `PROJECT_BASELINE.yaml` → `architecture_invariants` (+ `docs/architecture/ARCHITECTURE_INVARIANTS.md`) | ✅ |
| Final Gate Inventory | `PROJECT_BASELINE.yaml` → `final_gates` (+ `docs/architecture/FINAL_GATE_INVENTORY.md`) | ✅ |
| Implementation Plan | `PROJECT_BASELINE.yaml` → `implementation_plan` (+ `docs/architecture/IMPLEMENTATION_PLAN.md`) | ✅ |
| Version-sensitive Technology Baseline | §6 + `PROJECT_BASELINE.yaml` → `protocol_baselines` | ✅ |
| `PROJECT_BASELINE.yaml` | repository root | ✅ |

**Verification of Phase 0 itself:** the baseline is only meaningful if it is
true. Three of its claims were therefore falsified-or-confirmed by execution
rather than assertion:

1. Backend dependency set resolves and installs on 3.11.16 → **install exit 0**
2. All four infrastructure dependencies start and answer on their real
   protocols → **MySQL 8.4.11 alive / Redis PONG / Qdrant ok / MinIO live**
3. MCP SDK supports the spec's frozen protocol revision → **`LATEST_PROTOCOL_VERSION == '2026-07-28'`**

Phase 1 may proceed.
