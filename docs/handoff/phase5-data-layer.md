# Phase 5 handoff - data layer (data-layer)

Verified against the repo, not recalled. State when written: HEAD `e338eca`,
`alembic current` = `3f1ae2c55c54 (head)`, `alembic check` clean,
`python scripts/residue.py` -> `residue: 0`.

## 1. Files I own
- `payment/{enums,models,repository}.py` - `payments`, `payment_callbacks` (5 + 2 CHECKs).
- `fulfillment/{enums,models,repository}.py` - `fulfillments`, `fulfillment_items`; carrier allowlist (single declaration, per captain).
- `aftersales/{models,repository}.py` - `after_sales`, `after_sale_items`, `refunds`. **`aftersales/enums.py` is after-sales-workflow's - do not write it.**
- `order/models.py` - the two FG-12 caps only; otherwise the captain's.
- `migrations/versions/20260923_1643_3f1ae2c55c54_phase5_*.py` - the one Phase 5 migration.
- `tests/integration/commerce/{seed,residue,conftest}.py` + 3 test files; `scripts/residue.py`.
- `artifacts/evidence/integration/phase5_ddl_verification.md`.

## 2. Done and committed
`a785744` six tables + migration + DDL artifact + seed | `104ef26` `shipped_*` filters; added `planned_*` | `73d46fc` restored `test_seed_smoke.py` | `bd31fe9` residue tool | `e76dfe6` seed `addfinalizer` before first write + setup-failure proofs | `a3a6dd1`/`0a4e136`/`b73a6f4` `fulfillment_items.sku_id` | `9786a25` stale-docstring fix | `e338eca` probe staging fix | `dc3e9f8` phantom-workflow docstrings | `383168f`.

**NOT committed: nothing of mine.** `git status` shows only other writers' files.

## 3. Not done, in priority order
1. **`sku_id` UNRESOLVED; tree is at "column exists".** Captain ruled add/withdraw/add. Column is live `NOT NULL` and fulfilment's committed code reads and writes it (`serializers.py:75`, `service.py:319,744`). **Next: ask for one word - KEEP or REMOVE.** If REMOVE: revert `a3a6dd1`/`0a4e136`/`b73a6f4` **and** have fulfilment revert `row.sku_id` + both write sites in the same window. Recommended: KEEP.
2. **`sku_id` contradicts `REQ-FUL-002`** and `PHASE5_DESIGN` §5.4's corrected "no sku_id column" text. Both are the captain's files.
3. **Double-run equality not proven.** Needs `tests/integration/{order,fulfillment,aftersales}/conftest.py` to add, before the first write: `request.addfinalizer(lambda: purge_shop(created, marker=marker))` (`purge_shop` is reusable from `tests.integration.commerce.seed`). Their owners or captain's authorisation.
4. **`t8` was never created in the task list** - unowned; core work done in item `e76dfe6`.

## 4. Traps that cost real time
- **`op.create_check_constraint`/`op.drop_constraint` re-apply the naming convention** -> `ck_orders_ck_orders_refund_cap`; the downgrade then dropped a non-existent name (errno 3821) **after** `alembic_version` rolled back, and since MySQL DDL is non-transactional every later `upgrade` was a silent no-op. **Use `op.f(...)` on both.** Diagnose with `alembic downgrade <from>:<to> --sql`.
- **`CREATE TABLE ... LIKE` and `... AS SELECT` do NOT copy CHECK constraints on MySQL 8.4** - a probe built on either inserts cleanly and falsely reports "cap NOT enforced". Read the clause from `information_schema.CHECK_CONSTRAINTS` onto a key-free twin.
- **`STRICT_TRANS_TABLES`**: `ADD COLUMN ... NOT NULL` on a populated table fails. Do nullable -> backfill -> `MODIFY NOT NULL`.
- **A test count is meaningless without the DB state** (failed tests skip teardown). Measure with `scripts/residue.py`. **But do not over-apply:** six fulfilment failures I blamed on residue were a real flush-ordering defect exposed by my own change minutes earlier.
- **`BigIntUnsigned` rejects negatives by design** - use an empty `IN` list, not `[-1]`.
- **PowerShell `Set-Content` adds a BOM + CRLF** (whole-file diff). Use `UTF8Encoding($false)`, normalise `\n`.
- **`git add <dir>` ignores ownership**, and a concurrent `git reset` can clobber the index: commit with `git commit --include <pathspec>`.
- **`ruff` config discovery follows cwd** - `..\scripts\...` from `backend/` misses the per-file-ignores (phantom `T20`).
- **A pytest probe written inside a collected package self-replicates** if the child is killed. Stage under `tests/build/` (uncollected + gitignored).

## 5. Interfaces other modules depend on (do not change silently)
- `seed.py`: `Shop` (`consumer`, `staff`, `key(s)`, `client_request_id(s)`, `marker`, `merchant_id`, `sku_ids`, `warehouse_id`, `address_id`), `shop`, `paid_order(shop, *, lines=None, suffix=...)`, `make_order`, `settle_order`, `signed_success_callback`, `purge_shop(created, *, marker)`, `SKU_PRICES=(1999,2999,999)`, `OPENING_STOCK`, read-backs (`read_position`, `movements_for`, `load_order`, `status_logs`, `items_of`). `NV_TEST_SEED_MARKER` pins the marker.
- `FulfillmentRepository`: `shipped_quantities_for_order`/`shipped_quantity_for_order_item` **filter to SHIPPED/DELIVERED**; `planned_quantities_for_order` is the **unfiltered** counterpart. The 70001 guard and the order axis both read `shipped_*` - single implementation, do not add a second filtered total.
- `AfterSaleRepository`/`RefundRepository`: `get_by_after_sale_no_for_update`, `add`, `add_item`, `items_for`, `list_for_order`, `approve`, `reject`, `cancel`, `apply_refund`; `insert_succeeded` (**not** `add`), `stamp_refund_no`, `mark_failed`, `list_for_after_sale`, `total_for_after_sale` (**not** `total_refunded_...`).
- `Fulfillment.items`: `selectin`, `order_by=FulfillmentItem.id`, `cascade="all, delete-orphan"`.
- `residue.py`: `report_residue(session)`, `purge_test_residue(session, *, dry_run=True)`, `ResidueReport.line()`.

## 6. What I could not verify
- **Double-run equality** - blocked on three conftests (item 3); not proven.
- **Live-table cap enforcement** - my controls used twins carrying MySQL's own clause, not the real tables; FG-12 owns that (stated in the artifact's section 9).
- **`sku_id`'s final disposition** - held open by oscillating rulings; I did not act.
- **`alembic check` is unstable while tests run** - it transiently diffed the verifier's `_fg12_twin_payments`, then passed quiesced. Take gate readings on a quiet DB.
- **Residue counts are unstable under concurrent agents** - only marker-scoped counts are comparable.
- **I did not re-run the whole suite after the last three commits** (13.1). Last full measurement: `1094 passed, 0 failed, 0 errors` on a clean DB.

## 7. `sku_id` revert — attempt abandoned; exact state and the reliable recipe

`t7`'s `sku_id` column is **still in the tree** (HEAD `5d4c5f1`). The captain ruled it out
("remove sku_id, this reverses my own reversal"), and I tried and **failed to land the
revert**. Recording this honestly because the next agent should not repeat my approach.

**Why it is not a one-file change.** Three call sites in fulfilment's committed code depend
on the column, and they are not mine:

    fulfillment/serializers.py:75   sku_id=row.sku_id                    <- reads the column
    fulfillment/service.py:319      sku_id=line.sku_id                   <- create_shell writes it
    fulfillment/service.py:744      sku_id=sku_id                        <- residual package writes it

So removal needs the two commits above to be reverted **in the same window**, or
fulfilment's serializer raises `AttributeError` and both inserts fail `NOT NULL`. Two of
the captain's premises in that message are factually wrong and worth correcting before
anyone acts on them: `create_shell` **does** set the column (line 319, verified), and the
suites **were** green with it (fulfilment 38 passed, commerce 8 passed).

**What I actually tried, and why it failed.** Four attempts: regex deletion of the column /
FK / index / backfill blocks; then line-index deletion. Symptoms observed, in order:
writes that reported success but did not persist (the file was byte-identical to HEAD and
`git status` clean afterwards); one write that did land and left an `IndentationError` at
line 273; and line-index arithmetic that drifted on an 850-line file, deleting the
`ix_fulfillment_items_order_item_id` index as collateral. I verified after each attempt
rather than trusting the write, which is the only reason none of this reached a commit.
I then restored both files with `git checkout` so the tree stayed consistent — `alembic
check` clean, `alembic current` at head, model and migration agreeing.

**The reliable recipe for the next agent — do this first, do not repeat mine:**
1. `git checkout -- backend/app/modules/fulfillment/models.py backend/migrations/versions/20260923_1643_3f1ae2c55c54_phase5_payment_fulfillment_aftersales_and_refund.py`
   (start from the committed state; do not inherit my partial edits).
2. Edit the **model** with a targeted editor operation, not a regex: delete the
   `_sku_id_for_insert` helper, the `sku_id` `mapped_column(...)` block, and
   `Index("ix_fulfillment_items_sku_id", "sku_id"),`. Confirm with
   `python -c "from app.modules.fulfillment.models import FulfillmentItem as F; print(sorted(c.name for c in F.__table__.columns))"`.
3. **For the migration, prefer a NEW revision** that drops the index, the FK and the column
   (`op.drop_index`, `op.drop_constraint(type_="foreignkey")`, `op.drop_column`) over
   editing `3f1ae2c55c54` in place. In-place editing of an applied revision is what defeated
   me: it is an 850-line file and every anchor I chose drifted. A new revision is also the
   safer habit, since the applied one is referenced by the DDL evidence artifact.
   If you do edit in place, the four things to remove are the `sa.Column("sku_id", ...)`,
   the `sa.ForeignKeyConstraint(["sku_id"], ...)`, the `op.create_index("ix_fulfillment_items_sku_id", ...)`
   block, and the nullable → backfill → `ALTER ... MODIFY NOT NULL` block — and then
   `SHOW COLUMNS FROM fulfillment_items` must show 8 columns.
4. Only then tell fulfilment to revert `serializers.py:75` and `service.py:319,744`.

**Also unresolved, and smaller:** `PHASE5_DESIGN` §5.4 and `REQ-FUL-002` both deny the
column, so until the revert lands the schema contradicts two documents.
