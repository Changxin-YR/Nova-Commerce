# Handoff — Phase 6, increment 1: the transactional outbox

> **What this document is.** A complete, self-contained handoff of one working
> session: the exact repository state, what was implemented, what was *measured*
> versus what is merely *claimed*, everything known to be broken or unfinished,
> and the ordered next steps. Written for a reader (human or agent) with **zero
> prior context**.
>
> **Read this together with `HANDOFF.md` section 19**, which is the one-screen
> summary that points here. Everything in `HANDOFF.md` **before** section 19 is
> historical: section 18 describes Phase 5 at commit `ecf361f`, and its 18.1 line
> "`HEAD = ecf361f`, pushed" is no longer true.
>
> The long-form "why" for the code itself lives in the module docstrings under
> `backend/app/shared/outbox/` and `backend/app/shared/db/models/outbox.py`.
> Those are not decoration; they carry decisions that were paid for.

---

## 1. Repository state (exact)

| Fact | Value |
|---|---|
| Repository root | `C:\Users\27363\Desktop\store` |
| Remote | `git@github.com:Changxin-YR/Nova-Commerce.git` (SSH; HTTPS to github.com:443 is intermittent here) |
| Branch | `main` |
| `HEAD` | the tip of `main` - `git log -1 --format=%h` (this document is carried by a commit *after* `9354b6a`) |
| `origin/main` | `17e1184` |
| Sync | **NOT synced: `main` is 4 commits ahead of `origin/main`.** Nothing is pushed. |
| Working tree | **clean** (`git status --porcelain` is empty) |
| Ignored state dir | `.agent-teams/` (gitignored; the agent team used in this session has been stopped) |

### The four unpushed commits

| Commit | Subject | Scope |
|---|---|---|
| `075e293` | phase6 outbox: transaction-joining writer, publisher, and the three wired seams | 10 files, +1101 / -29 |
| `cb62f4a` | outbox tests: the suite, the false-collision guard, and the teardown obligation | 11 files, +2064 / -8 |
| `b190ea3` | docs: the outbox seam is wired, and the position claim is corrected | 1 file, +39 / -13 |
| `9354b6a` | handoff: Phase 6 outbox increment - state, open items, and the traps | 2 files, +329 |

The complete list is `git log --oneline origin/main..main`. The last entry is the
commit that added *this* document, so both the tip and the count move when it is
committed - which is why they are given as a command here rather than a number.

### Measured state at `HEAD`

| Check | Command | Result |
|---|---|---|
| Full suite | `python -m pytest tests -q` (from `backend/`) | **1137 passed, 0 failed**, 1 warning (starlette `anyio.abc.BlockingPortal` deprecation), 94.11 s |
| Lint | `python -m ruff check --no-cache app tests migrations` | `All checks passed!` |
| Schema head | `python -m alembic current` | `507bb852a092 (head)` |
| Schema drift | `python -m alembic check` | `No new upgrade operations detected.` |
| DB residue | `python scripts/residue.py` | `residue: 0 (no rows attributable to a fixture marker)` |
| Containers | `docker compose --env-file .env -f ops/docker-compose.yml ps` | 5/5 `running (healthy)`: `nx-mysql`, `nx-redis`, `nx-qdrant`, `nx-minio`, `nx-keycloak` |

`1137 = 1102` (before this increment) `+ 35` (the new `tests/integration/outbox/`).

**Provenance of the numbers.** The `pytest` run was taken on the working tree
immediately before commits `075e293`/`cb62f4a`; the code content of those paths is
byte-identical to what is committed at `HEAD` (the only later commits touch
`docs/` and `HANDOFF.md`). `ruff`, `alembic` and `residue` were re-checked after
the commits. If you want a fresh number, re-run section 9.

---

## 2. What this increment implemented

Scope: **spec section 49 / `REQ-CON-003`** — the transactional outbox. One
table, one writer, one publisher, and three call sites. Phase 6 as a whole is much
larger; see section 6.

### 2.1 The table

`backend/migrations/versions/20260923_2110_507bb852a092_phase6_outbox_messages.py`
(revises `3f1ae2c55c54`, the Phase 5 revision).

`outbox_messages` — 14 columns. The important ones:

| Column | Type | Note |
|---|---|---|
| `event_type` | `varchar(64)` | `order.created` / `payment.settled` / `refund.succeeded` |
| `aggregate_type` | `varchar(32)` | `order` / `payment` / `refund` / `after_sale` |
| `aggregate_id` | `bigint unsigned` | the aggregate's own primary key |
| `idempotency_key` | `varchar(128)` | the **emitter's** key, verbatim (traceability, indexed — **not** the uniqueness anchor) |
| `payload` | `json` | non-sensitive summary only |
| `status` | `varchar(16)` | `PENDING` / `PUBLISHED` / `FAILED` / `DEAD`, with a hand-written `CHECK` |
| `attempt_count` | `int` | starts at **0** (it counts *delivery* attempts) |
| `next_retry_at`, `published_at` | `datetime(3)` | the retry clock |
| `last_error` | `varchar(500)` | bounded so a transport's error text cannot fail the insert meant to record it |
| `merchant_id` | `bigint unsigned` | `MerchantScopedMixin`, **RESTRICT FK onto `merchants`** — see section 5.1, this is load-bearing for test teardown |
| `created_at`, `updated_at` | `datetime(3)` | |

Key constraints: `UNIQUE (event_type, aggregate_type, aggregate_id)` (the dedup
anchor — section 7), `CHECK status IN (...4 states)`,
`INDEX (status, next_retry_at)` (the publisher's scan),
`INDEX (event_type, idempotency_key)` (an operator's grep).

The migration was **autogenerated then hand-reviewed**, and the result was read
back from `information_schema` rather than trusted:

```
CHECK        ck_outbox_messages_status_valid      (status in ('PENDING','PUBLISHED','FAILED','DEAD'))
FOREIGN KEY  fk_outbox_messages_merchant_id_merchants   -> merchants.id  ON DELETE RESTRICT
PRIMARY KEY  PRIMARY (id)
UNIQUE       uq_event_type_aggregate              (event_type, aggregate_type, aggregate_id)
INDEX        ix_outbox_messages_status_next_retry_at    (status, next_retry_at)
INDEX        ix_outbox_messages_created_at              (created_at)
INDEX        ix_outbox_messages_merchant_id             (merchant_id)
INDEX        ix_outbox_messages_event_type_idempotency_key (event_type, idempotency_key)
```

`downgrade()` is a single `op.drop_table("outbox_messages")` on purpose: an
explicit `op.drop_index` first is what broke the Phase 4 downgrade with errno
1553 (the FK's supporting index is removed by `DROP TABLE`). Autogenerate still
emits the drops; they were removed by hand.

### 2.2 The writer — `backend/app/shared/outbox/writer.py`

`OutboxWriter(session).enqueue(event_type, aggregate_type, aggregate_id,
idempotency_key, payload, merchant_id=None) -> OutboxMessage`.

* **Appends to the caller's transaction. Never commits, never opens one.** This
  is the entire point: the event row and the business rows must become visible
  together or not at all. If the caller rolls back, the event was never queued.
* Raises `ValueError` on an unknown `event_type`/`aggregate_type` (before writing).
* Raises `ValueError` if the payload names a secret-bearing key, checked with
  `app.core.redaction.assertion_that_no_secret_remains` (**before** writing —
  the outbox is the last place a leak can be caught).
* Attempts the insert and treats the unique violation as "already queued"
  (`SAVEPOINT` via `begin_nested()`, the same idiom as
  `IdempotencyRepository.insert_in_progress`). The re-read uses the **same triple
  as the unique**; if a concurrent *uncommitted* winner is invisible
  (`READ COMMITTED`), it re-raises the original `IntegrityError` rather than
  losing the cause.

### 2.3 The publisher — `backend/app/shared/outbox/publisher.py`

* `publish_due(session, *, transport, now=None, limit=100, max_attempts=5,
  base_backoff_seconds=30, max_backoff_seconds=3600) -> PublishOutcome(published,
  failed, dead)`. Selects `status IN (PENDING, FAILED)` and
  `next_retry_at IS NULL OR <= now`, ordered by id, `LIMIT`,
  **`FOR UPDATE SKIP LOCKED`**. Locks rows, hands each to the transport, records
  the outcome, and **flushes but does not commit** (the caller owns the boundary,
  as everywhere else in this codebase).
* Retry arithmetic: `attempt_count` is incremented **before** the delivery is
  attempted (so a crash mid-delivery is counted); on failure the row goes to
  `FAILED` with `next_retry_at = now + base * 2**(n-1)` capped; at
  `attempt_count >= max_attempts` it goes to terminal `DEAD`.
* `run_publish_cycle(...)` is the worker entry point and **does** own its
  transaction (`session_scope()`).
* `OutboxTransport` is a `Protocol` (`deliver(message)` returns or raises).
  `LoggingTransport` is the only implementation — **a deliberate
  placeholder**, see section 5.4.

### 2.4 The three seams — now calls, not comments

| Workflow | Step | Event | `aggregate_type` / `aggregate_id` | `idempotency_key` |
|---|---|---|---|---|
| `CreateOrderWorkflow` (`app/modules/order/workflow.py`) | 9 | `order.created` | `order` / `order.id` | the request's `Idempotency-Key` |
| `PaymentSuccessWorkflow` (`app/modules/payment/workflow.py`) | 10 | `payment.settled` | `payment` / `payment.id` | `f"{provider}:{event_id}"` |
| `RefundWorkflow` (`app/modules/aftersales/workflow.py`) | 7 | `refund.succeeded` | `refund` / `refund.id` | the refund's own `Idempotency-Key` |

Payload shapes are built by functions in
`backend/app/shared/outbox/contract.py` and are identifiers + amounts + statuses
only. `refund.succeeded` carries exactly the ten keys the Phase 5 seam comment
named (`refund_no`, `refund_id`, `after_sale_no`, `order_no`, `amount`,
`order_refunded_amount`, `order_paid_amount`, `payment_status`,
`after_sale_status`, `claim_status`).

**None of the three call sites moved.** They sit where Phase 5 put them: after
the work that decides the operation happened, before the single `commit()`.

### 2.5 Test-support changes the write side forced (the teardown obligation)

`outbox_messages.merchant_id` is a RESTRICT FK, so every test cleanup that
deletes a merchant must delete that merchant's outbox rows first, or it dies with
errno 1451 part-way through an untransactional purge. Changed for this:

* `backend/tests/integration/commerce/seed.py` -> `purge_shop`
* `backend/tests/integration/commerce/residue.py` -> `report_residue` (counts so a
  leak is visible — `is_clean` sums `counts`) and `purge_test_residue`
* `backend/tests/integration/{order,payment,fulfillment}/conftest.py`
* `backend/tests/concurrency/test_payment_idempotency.py` (its own purge and its
  own orphan-marker sweep)

While doing this, a **pre-existing** leak was closed: `purge_shop` swept
`payment_callbacks` by marker only, so a callback whose `provider_event_id` did
not embed the marker outlived teardown forever — measured at **26 rows
accumulating, one per run**, surfacing in `scripts/residue.py` as
`orphaned fixture callbacks (order gone): N`. It now also sweeps by
`merchant_id`.

### 2.6 Gate-adjacent change

`backend/tests/concurrency/test_payment_idempotency.py` clause 8 was upgraded
from the proxy ("exactly one settlement, exactly one PROCESSED callback") to the
**direct** assertion: exactly one `payment.settled` row in `outbox_messages`.
That is what `REQ-PAY-004` clause 4 actually says. It is also why the FG-11
artifact is now stale (section 3).

---

## 3. Verification: what was measured

| Claim | How it was measured | Result |
|---|---|---|
| The suite is green | `python -m pytest tests -q` | 1137 passed, 0 failed |
| The 35 new tests are green | `python -m pytest tests/integration/outbox -q` | 35 passed (~10 s, repeatable) |
| Lint is clean | `ruff check --no-cache app tests migrations` | clean |
| The migration matches the models | `alembic check` | no new operations |
| The table is really there, as declared | `information_schema` read-back (columns, CHECK, UNIQUE, FK, indexes) | matched |
| The migration is reversible | `alembic downgrade -1` then `upgrade head` (performed) | both ran |
| The write side leaves no residue | `scripts/residue.py` before/after | `residue: 0` both |
| Existing tests still pass with the seams wired | the full suite above | 1137 passed |
| FG-11 still passes with clause 8 strengthened | `pytest tests/concurrency/test_payment_idempotency.py -m concurrency` | 11 passed |
| FG-12 still passes | `pytest tests/integration/refund tests/concurrency/test_refund_concurrency.py -m "integration or concurrency"` | 13 passed |

### Mutation checks (performed by the test author, not independently)

18 mutations, each of which turned at least one test red, then reverted with the
files hash-verified byte-identical. They included: `commit()` inside `enqueue`;
moving `attempt_count += 1` after delivery; linear backoff; `DEAD` at `>` instead
of `>=`; the selection ignoring `next_retry_at`; `commit()` inside `publish_due`;
dropping `uq_event_type_aggregate` (a **real `ALTER TABLE`**); replacing the
unique with one on `(event_type, idempotency_key)`; and six seam mutations.

**This is author-side evidence, not independent verification.** See section 4.1.

---

## 4. What is NOT verified (do not over-claim)

### 4.1 No independent verification of this increment

A verifier role was assigned and was **interrupted before finishing**. The 18
mutations above are the author's own. Do not report this increment as
independently verified.

### 4.2 No multi-worker publisher test

`FOR UPDATE SKIP LOCKED` appears in the selection and in the publisher's
docstring, but **nothing in the 35 tests starts two publishers at once**. The
property the clause exists for — two workers stepping over each other's rows
rather than double-delivering — is currently asserted only by reading the
code. This is the most valuable missing test.

### 4.3 No real transport, no Celery app, no schedule

`LoggingTransport` always succeeds. The publisher's *contract* is exercised
(`deliver` returns -> `PUBLISHED`; raises -> `FAILED`/`DEAD`), but there is no
broker, no `celery` app object, and no beat entry point. `pyproject.toml` already
depends on `celery==5.6.3` and `app/core/config.py` already has
`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` / `CELERY_TASK_*`, so the wiring is
the missing part, not the dependency.

### 4.4 No reconciliation jobs (spec section 50)

The outbox-retry half of section 50 is implemented (`next_retry_at`); the
scheduler half is not. Still absent: expired-order closure, coupon expiry,
expired pending actions, knowledge-ingestion recovery.

### 4.5 The FG-11 clause-8 "masked mutation"

Moving the `payment.settled` emit block **above** its guards does **not** redden
FG-11 on its own, because `enqueue`'s aggregate dedup absorbs the duplicate. This
was measured — by making `uq_event_type_aggregate` ineffective with a real
`ALTER TABLE` at the same time. **A green run there is a masked mutation, not
evidence that the position is irrelevant.** The seam comment and
`ORDER_WORKFLOW.md` section 12.2 now say so.

### 4.6 Stale gate evidence

Both mandatory-gate artifacts now describe a revision that no longer exists:

| Gate | Artifact | Stamped at | Its `relevant_paths` (all now changed) |
|---|---|---|---|
| FG-11 | `artifacts/evidence/concurrency/fg11_payment_idempotency.json` | `0e2b0ec` | `backend/app/{core,shared,main.py,api}`, `backend/app/modules/{payment,fulfillment,order,inventory}`, `backend/migrations`, `backend/tests/concurrency`, `backend/tests/conftest.py` |
| FG-12 | `artifacts/evidence/integration/fg12_refund_invariants.json` | `62cad40` | `backend/app`, `backend/migrations`, `backend/tests/integration/refund`, `backend/tests/concurrency/test_refund_concurrency.py`, `backend/tests/integration/commerce` |

FG-09 (`concurrency/fg09_inventory_over_sell.json`) and FG-10
(`integration/fg10_workflow.{json,xml}`) are also older than `HEAD`, but this
increment did not change the paths they watch — **verify that claim before
trusting it**, it is exactly the kind of statement that goes stale.

`scripts/gate_evidence.py` sets a dirty flag when the watched paths differ from
`HEAD`, so these must be re-emitted **from a clean tree, after the last commit**,
never before.

---

## 5. Known problems and risks, in priority order

### 5.1 The teardown obligation keeps arriving (structure, not a bug)

Any new test cleanup that deletes a `merchants` row must first delete that
merchant's `outbox_messages` rows. It is fixed for every suite that exists today.
`tests/concurrency/test_inventory_oversell.py` deletes a merchant **without**
touching the outbox and is safe **only because FG-09 never runs
`CreateOrderWorkflow`**. If that changes, that teardown breaks with errno 1451.
The general rule: a table with a FK onto `merchants` inherits this.

### 5.2 `scripts/residue.py` checks only one direction

It detects *child-with-missing-parent* (`orphaned_callbacks`) but has no
*parent-with-missing-child* check — a balance whose ledger rows went, or an
`inventories` row `verify_ledger` (INV-007) cannot explain. `HANDOFF.md` section
18.5 item 4 carries the written-out query. It also **cannot see a missing index or
constraint**, which is why the DDL read-back is a manual step.

### 5.3 Per-process schema isolation still does not exist

One `nova` schema, no isolation between processes. Serialise `pytest` runs. The
tell for a violation is that the failing set **moves between runs** and the tests
pass in isolation. (`HANDOFF.md` section 18.4 item 1.)

### 5.4 The publisher's transport is a placeholder

`LoggingTransport` is honest about it in its docstring. What is missing is the
real thing, plus the decision about delivery semantics (at-least-once with
consumer-side dedup is the assumption the design implies, and it should be
written down rather than inferred).

### 5.5 Pre-existing: some committed files contain GBK mojibake

`backend/app/modules/order/workflow.py` contains **38 occurrences of a CJK
character (U+6402, rendered `(搂)`) where `§` was intended**, and they are
in the **committed blob** — not editor damage, and not caused by this session.
A `§`-based search/replace fails against those lines. (Verified by reading
`git show HEAD:<path>` as bytes: zero `C2 A7` and zero `E6 8E 82`; the comment
bytes are GBK-encoded garbage carried into a UTF-8 file.) This increment added one
correct `§` next to them, which makes the inconsistency visible. Fixing the
rest is a separate, reviewable change (comments only) — it is **not** done.

### 5.6 Pre-existing: `FINAL_GATE.md` is very stale

It still says `Current phase: Phase 3b complete (FG-09 green) - Phase 4 next` and
`Base commit: ea241050...`. It is a **generated echo** and is supposed to be
rebuilt by `python scripts/final_gate.py --evidence-dir artifacts/evidence --out
FINAL_GATE.md`; regenerating it requires the gate artifacts in section 4.6 to be
current first. Nothing in this increment touched it.

### 5.7 Pre-existing: `PROJECT_BASELINE.yaml` phase statuses are stale

`implementation_plan.phases[*].status` says phase 0 `COMPLETE` and phases 1-16
`PENDING`, although phases 1-5 are in fact complete. It has apparently not been
maintained per phase; do not read it as a progress report. Its `requirements` and
`gates` sections are authoritative and are what you should read.

### 5.8 Pre-existing: `HANDOFF.md` before section 19 is historical

Sections 1-18 describe earlier phases. Section 18.1's "`HEAD = ecf361f`, pushed"
is a stale claim, not a current one. Section 19 is the current statement of state.

### 5.9 Documentation debt carried from Phase 5 (section 18.7a)

Five small prose refinements were agreed in the previous session and recorded but
**not applied**: two in `docs/handoff/phase5-payment-workflow.md`, one about the
`providers.py` docstring, one in `docs/handoff/phase5-after-sales.md` (the forward
obligation about `payment_callbacks`), and the `residue.py`
parent-with-missing-child check. See `HANDOFF.md` section 18.7a. Note that its
item 4 ("if a future edit makes the refund or claim path write
`payment_callbacks`, the teardown obligation arrives with it") is a *sibling* of
section 5.1 here, and this increment is the worked example of exactly that hazard.

---

## 6. Project-wide phase status

`PROJECT_BASELINE.yaml` plans 17 phases; the mandatory gates are listed in
`docs/architecture/FINAL_GATE_INVENTORY.md`.

| Phase | Name | Status |
|---|---|---|
| 0 | Audit + Baseline Lock | complete |
| 1 | Skeleton + Infrastructure + MinIO | complete |
| 2 | Identity + Auth Session + RBAC + DataScope | complete |
| 3 | Catalog + SKU + Inventory | complete (FG-09 green) |
| 4 | Cart + Pricing + Order | complete (FG-10 artifact exists) |
| 5 | Payment + Fulfillment + AfterSales + Refund | complete (FG-11, FG-12 green — artifacts need re-emission) |
| **6** | **Marketing + Analytics + Outbox** | **in progress — the outbox landed (section 2); Marketing and Analytics have not started** |
| 7 | Consumer Vue | notes exist (`docs/architecture/PHASE7_ORDER_INTEGRATION_NOTES.md`); the frontend is present and configured (`vue-tsc`, `vitest`) but is not covered by this increment |
| 8-16 | Merchant Console ... Final Gate | not started |

Gate artifacts currently committed: FG-09, FG-10, FG-11, FG-12. The rest
(FG-01...FG-08, FG-13...FG-26) have no artifact yet.

### What Phase 6 still owes

* **Marketing** — `promotions` + `promotion_products` with the Strategy
  Pattern over `rule_config` (section 39, `REQ-MKT-001`); `coupon_templates`,
  `user_coupons`, `coupon_usage_records` with `UNUSED/LOCKED/USED/EXPIRED`
  (section 40, `REQ-MKT-002`); coupon **lock-on-order / use-on-payment /
  release-on-cancel** (`REQ-MKT-003`). The order path already carries the seam:
  `validate_coupon_input` refuses a coupon it has no rule for, and `PricingRules`
  is the internal interface Phase 6 fills. No `marketing` module exists yet under
  `backend/app/modules/`.
* **Analytics** — nothing exists.
* **Outbox, remaining** — Celery app + beat entry point wrapping
  `run_publish_cycle`; a real transport; the section 50 reconciliation jobs; a
  two-worker concurrency test (section 4.2).
* **FG-08** is Phase 6's gate.

---

## 7. The one design decision that departs from the seam text

The Phase 5 seam comment said the row "must be written with the refund's own
idempotency key". That key **is** stored (`idempotency_key`, indexed with
`event_type`) — but it is **not the uniqueness anchor**. The unique is:

```
UNIQUE (event_type, aggregate_type, aggregate_id)
```

**Why.** An emitter key is only unique *inside its own scope*: an order's
`Idempotency-Key` is unique within `idempotency_records` (a **client-supplied**
string two users may both send); a refund's is unique **per merchant**; a payment
event's is unique **per provider**. A **global** unique on any of those silently
drops a legitimate second event whenever two scopes happen to pick the same
string — and that failure direction is the dangerous one: **a lost event is
invisible** (no error, no log, nothing `residue.py` or a foreign key can observe)
while a duplicate is loud.

The aggregate triple cannot collide by construction — `aggregate_type` names
the table and `aggregate_id` is that table's primary key — and "an order is
created once" / "a refund succeeds once" is exactly the set of facts that must
not be recorded twice.

The test that guards this is
`test_two_aggregates_sharing_one_idempotency_key_yield_two_rows` — the
**only** test that catches the false-collision direction.

**If you disagree, say so explicitly and say what changes.** Do not quietly
revert the constraint to the emitter key; that is the change that loses events.

---

## 8. Environment and tooling

### 8.1 Database and containers

`ops/docker-compose.yml` (run with `--env-file .env`). Five services, all
`healthy`: MySQL 8.4 (`nx-mysql`, host port `13306`, database `nova`), Redis 7.4
(`nx-redis`, `16379`), Qdrant (`nx-qdrant`, `16333/16334`), MinIO (`nx-minio`,
`19000/19001`), Keycloak (`nx-keycloak`, `18080`).

Use the project venv: `.venv\Scripts\python.exe` (Python 3.11.16). Run `pytest`
from `backend/`.

### 8.2 Scripts

| Script | Purpose |
|---|---|
| `scripts/gate_evidence.py` | shared core: runs a real pytest target, parses pytest's own report + JUnit, records git state (including `relevant_paths_dirty`), writes the artifact |
| `scripts/emit_fg09_evidence.py`, `emit_fg10_evidence.py`, `emit_fg11_evidence.py`, `emit_fg12_evidence.py` | per-gate emitters |
| `scripts/residue.py` | report (default) / `--purge` fixture residue; **exit 1 when residue found**, so a gate can gate on it |
| `scripts/db_residue_report.py` | DB-level residue report |
| `scripts/verify_refund_caps.py` | FG-12 support |
| `scripts/final_gate.py` | regenerates `FINAL_GATE.md` from the artifacts |

### 8.3 Agent-team state (this session)

The session used one agent team (`phase6-outbox`, working dir
`.agent-teams/phase6-outbox`, gitignored). Members: `seams-author` (completed the
test suite, removed) and `verifier` (interrupted, removed). Two tasks were on the
board: `t1` (test suite) **completed**; `t2` (independent verification)
**requeued/unfinished**. No agent process is running now; the database was left
clean (`residue: 0`). The team's state directory is tool state, not project
artifacts — safe to ignore or delete.

---

## 9. First commands for the next conversation

```powershell
cd C:\Users\27363\Desktop\store
git log --oneline -6
git status -sb                       # expect: clean, ahead 4 of origin/main
ssh -T git@github.com                # SSH is the working remote
cd backend
& ..\.venv\Scripts\python.exe ..\scripts\residue.py        # record this line with any number you report
& ..\.venv\Scripts\python.exe -m alembic current           # expect 507bb852a092 (head)
& ..\.venv\Scripts\python.exe -m pytest tests -q           # expect 1137 passed
& ..\.venv\Scripts\python.exe -m ruff check --no-cache app tests migrations
& ..\.venv\Scripts\python.exe -m alembic check
cd .. ; docker compose --env-file .env -f ops/docker-compose.yml ps
```

Then read, in this order: `HANDOFF.md` section 19, this file,
`PROJECT_BASELINE.yaml` (the section 6 requirement set),
`docs/architecture/API_CONTRACT.md`, `docs/architecture/ORDER_WORKFLOW.md`
sections 11-12, and `docs/handoff/phase5-*.md`.

### Ordered next steps

1. **Decide whether to push.** Four verified commits sit only on this machine
   (`git push`). Everything downstream — the artifacts in step 2 especially
   — is cleaner with `origin` in step.
2. **Re-emit FG-11 and FG-12 from a clean tree** (they are stale, section 4.6):
   `python scripts/emit_fg11_evidence.py`, `python scripts/emit_fg12_evidence.py`.
   Confirm each artifact's `git.relevant_paths_dirty` is `false` afterwards. Then
   consider `python scripts/final_gate.py --evidence-dir artifacts/evidence --out
   FINAL_GATE.md` (section 5.6), and FG-09/FG-10 if their watched paths moved.
3. **Get the independent verification that `t2` never finished** (section 4.1)
   — especially a two-worker publisher test (section 4.2), the real gap.
4. **Then** resume Phase 6 proper: Marketing (section 6) — start with the
   coupon resolver behind `PricingRules` / `validate_coupon_input`, because the
   order path's seam already exists and is the reason it is cheap now.
5. Optionally clear the small debts: apply section 18.7a's five prose refinements
   (section 5.9), and the `residue.py` second-direction check (section 5.2).

---

## 10. Traps that cost time here (read before editing)

1. **Line endings are a live hazard.** `core.autocrlf=true` in this checkout, and
   Python's default text-mode write translates `\n` to `\r\n`. A mutation-restore
   that used `pathlib.write_text()` rewrote two whole files as CRLF (374 spurious
   bytes) while a **text** diff reported **zero differences** —
   universal-newline handling ate the evidence. Use `read_bytes()` /
   `open(p, "wb")` for anything that must round-trip, and verify with a byte hash
   plus `git diff --stat` (a whole-file `--stat` means you tripped it).
2. **Some committed files are GBK mojibake** (section 5.5). Do not conclude a tool
   corrupted them; check `git show HEAD:<path>` first.
3. **One `nova` schema.** Serialise `pytest` runs; concurrent runs fail on rows a
   fixture just committed, and the failure set moves between runs.
4. **`alembic` can be mid-state.** Read `alembic current` before any re-run; a
   reading of "every Phase 5 table MISSING" is usually a downgrade, not a code
   fault.
5. **`commit -F <file>` with PowerShell `-Encoding utf8` writes a BOM**, and the
   BOM lands in the commit subject. Write message files with
   `[System.IO.File]::WriteAllText($p, $msg, (New-Object System.Text.UTF8Encoding($false)))`.
   (This project has been bitten by a BOM in a heading before.)
6. **`session.flush()` is not `session.commit()`.** `publish_due` and `enqueue`
   deliberately stop at the flush. If a test reads a row "before the commit" and
   passes, suspect a shared session rather than a passing transaction — every
   read-back helper here opens a **fresh** session on purpose.

---

## Appendix A — file inventory of the increment

**New**

```
backend/app/shared/db/models/outbox.py                                   (+212)
backend/app/shared/outbox/__init__.py                                    (+74)
backend/app/shared/outbox/contract.py                                    (+152)
backend/app/shared/outbox/publisher.py                                   (+230)
backend/app/shared/outbox/writer.py                                      (+144)
backend/migrations/versions/20260923_2110_507bb852a092_phase6_outbox_messages.py  (+166)
backend/tests/integration/outbox/__init__.py                             (+13)
backend/tests/integration/outbox/conftest.py                             (+254)
backend/tests/integration/outbox/test_publisher.py                       (+665)
backend/tests/integration/outbox/test_seams.py                           (+593)
backend/tests/integration/outbox/test_writer.py                          (+445)
docs/handoff/phase6-outbox.md                                            (this file)
```

**Modified**

```
backend/app/shared/db/models/__init__.py            export the outbox model
backend/app/modules/order/workflow.py               step 9 wired
backend/app/modules/payment/workflow.py             step 10 wired
backend/app/modules/aftersales/workflow.py          step 7 wired
backend/tests/integration/commerce/seed.py          purge_shop: outbox + merchant-scoped callbacks
backend/tests/integration/commerce/residue.py       report + purge outbox
backend/tests/integration/order/conftest.py         teardown order
backend/tests/integration/payment/conftest.py       teardown order
backend/tests/integration/fulfillment/conftest.py   teardown order
backend/tests/concurrency/test_payment_idempotency.py  clause 8 + teardown
docs/architecture/ORDER_WORKFLOW.md                 sections 11.5 / 12.2
HANDOFF.md                                          section 19
```

## Appendix B — the 35 new tests

`test_writer.py` (9): `test_a_rolled_back_create_leaves_no_event_row`,
`test_enqueue_inside_a_rolled_back_transaction_writes_nothing`,
`test_the_same_aggregate_twice_yields_one_row`,
`test_two_aggregates_sharing_one_idempotency_key_yield_two_rows`,
`test_a_payload_naming_a_secret_is_refused_and_writes_nothing`,
`test_an_unknown_aggregate_type_is_refused`,
`test_an_unknown_event_type_is_refused`,
`test_the_caller_can_keep_using_its_session_after_a_dedup_collision`,
`test_the_writer_never_sets_a_publication_state`.

`test_publisher.py` (12):
`test_a_pending_event_is_published_once_with_exactly_one_attempt`,
`test_a_second_cycle_does_not_redeliver_a_published_row`,
`test_limit_bounds_one_cycle`, `test_run_publish_cycle_commits_by_itself`,
`test_a_failed_delivery_backs_off_and_is_invisible_before_next_retry_at`,
`test_the_backoff_doubles_per_attempt_and_is_capped`,
`test_consecutive_failures_reach_dead_at_max_attempts`,
`test_max_attempts_is_honoured_when_the_caller_lowers_it`,
`test_a_transport_error_is_recorded_and_truncated_to_the_column`,
`test_a_non_exception_error_does_not_stop_the_batch`,
`test_a_row_with_published_at_is_never_selected_by_a_later_clock`,
`test_the_publisher_does_not_commit_for_the_caller`.

`test_seams.py` (9): `test_order_created_is_emitted_once_by_the_real_workflow`,
`test_a_replayed_order_create_emits_no_second_event`,
`test_a_failed_create_emits_no_order_event`,
`test_payment_settled_is_emitted_once_and_a_duplicate_delivery_adds_nothing`,
`test_a_refused_callback_emits_no_payment_event`,
`test_refund_succeeded_carries_the_ten_contract_keys`,
`test_a_replayed_refund_emits_no_second_event`,
`test_no_seam_payload_carries_a_sensitive_key`,
`test_provider_event_ids_carry_the_fixture_marker`.

(9 + 12 + 9 = 30 test functions; 35 collected with parametrisation.)

**Still missing** (see section 4.2): a two-worker publisher race.