# Architecture Invariant Inventory

> Frozen by spec §112. Canonical machine-readable copy: `PROJECT_BASELINE.yaml` → `architecture_invariants`.
>
> **An invariant is only real if something fails when you break it.** Each row
> below names the enforcement mechanism. An invariant with no executable
> enforcement is a comment, not an invariant.

| ID | Invariant | Enforced by | Gate |
|---|---|---|---|
| INV-001 | `available_qty >= 0` | MySQL `CHECK` constraint + pessimistic lock + concurrency test | FG-09 |
| INV-002 | `locked_qty >= 0` | MySQL `CHECK` constraint + concurrency test | FG-09 |
| INV-003 | The same stock can never be deducted twice | `inventory_movements.idempotency_key` UNIQUE + movement appender | FG-11 |
| INV-004 | One payment event ⇒ exactly one business effect | `UNIQUE(provider, provider_event_id)` + `payments.idempotency_key` + transactional outbox | FG-11 |
| INV-005 | `refunded_amount <= paid_amount` | RefundService guard, re-read inside the transaction + DB `CHECK` | FG-12 |
| INV-006 | `Order.payable_amount == SUM(OrderItem.payable_amount)` | Discount allocation algorithm + assertion after persist | FG-10 |
| INV-007 | InventoryMovement fully explains every stock change | before/after columns on every movement + reconciliation job | FG-09 |
| INV-008 | Payment success never originates from a user or agent assertion | Callback signature verification; mock pay is DEV/DEMO only; no service method accepts a "mark paid" from the agent path | FG-14 |
| INV-009 | An unapproved PendingAction can never write business data | Tool gateway requires a `SUCCEEDED`-eligible approved action; the executor refuses any other state | FG-15 |
| INV-010 | Agent effective permission ⊆ current user permission | `ToolGateway` intersection: agent allowlist ∩ user permission ∩ runtime enabled | FG-14 |
| INV-011 | The Analytics agent can never write | Per-agent allowlist constructed from read-only tools only + architecture test | FG-14, FG-25 |
| INV-012 | MCP scope can never bypass RBAC or DataScope | MCP context derives identity server-side; effective permission = scope ∩ RBAC ∩ DataScope ∩ tool policy | FG-18 |
| INV-013 | Retrieved RAG content can never trigger a business write | Evidence is treated as data, never instruction; the knowledge path holds no write-capable tool | FG-17 |
| INV-014 | Historical order snapshots are immune to later product edits | `order_items` snapshot columns; no FK-driven live reads on the order path | FG-10 |
| INV-015 | The same `Idempotency-Key` never produces a duplicate result | `idempotency_records` UNIQUE(scope, idempotency_key) + request hash comparison | FG-08, FG-11 |
| INV-016 | After refresh rotation the old refresh token is dead | `auth_sessions.rotated_from` + `revoked_at`; rotation is single-use | FG-13 |
| INV-017 | A missing object in storage can never be reported READY | Ingestion re-checks object existence; READY requires a verified checksum round-trip | FG-19 |
| INV-018 | A LangGraph resume never repeats a business side effect | `CreatePendingAction` is a separate node from `AwaitApproval`; the pre-interrupt node body is side-effect free and idempotent | FG-15, FG-25 |

---

## How "enforced by" is kept honest

Spec §136 requires automated architecture constraint checks, and spec §149
forbids theoretical completion. Accordingly:

- **DB-level** invariants are written as real `CHECK`/`UNIQUE` constraints in
  Alembic migrations, so a violation raises an `IntegrityError` rather than
  being quietly accepted.
- **Service-level** invariants are asserted inside the same transaction that
  performs the write, so they cannot be raced.
- **Boundary** invariants (INV-008 … INV-013) are asserted by import-graph and
  registry inspection in `tests/architecture/`, which fails the build if a
  forbidden edge appears — not by code review.
- **Concurrency** invariants (INV-001 … INV-004) are proven against **real
  MySQL**, never a mock. Spec §113 is explicit: *"禁止 Mock DB 后宣称通过"*.

`tests/architecture/test_invariants_registry.py` asserts that every `INV-*` id
in this file has at least one referenced test, so an invariant cannot be added
here and silently forgotten.
