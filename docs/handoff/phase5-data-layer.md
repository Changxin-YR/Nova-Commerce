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

## 7. sku_id: KEPT - the column stays (final ruling, do not revert)

**Superseded: an earlier version of this section described a failed revert attempt and a
recipe for completing it. The captain cancelled that revert as their final word, so the
recipe below is retained only as a record of what NOT to redo.** Do not act on it.

The column is present and correct in all three places:
`
model      fulfillment_items.sku_id  (nullable=False, FK product_skus.id RESTRICT, indexed)
migration  20260923_1643_3f1ae2c55c54  sa.Column(sku_id, ...) present
live DB    SHOW COLUMNS -> fulfillment_id, order_item_id, sku_id, product_name, sku_name,
                            quantity, id, created_at, updated_at
alembic check -> No new upgrade operations detected.   alembic current -> 3f1ae2c55c54 (head)
`
Fulfilment's committed code depends on it and must keep doing so:
serializers.py:75 reads 
ow.sku_id; service.py:319 (create_shell) and
service.py:744 (residual package) write it. order/serializers.py derives the same field
on the order read path from a required sku_by_line map. **Both paths are correct as they
stand** - do not unify them; the module boundary is deliberate (the fulfilment module must
not reach into the order module's serializer).

**Why the column is legitimate, and the reading of the baseline that settles it.** The
captain first withdrew the column on the argument that REQ-FUL-002 enumerates
ulfillment_id, order_item_id, quantity and so forbids anything else. That reading is
wrong: the same entry omits id, created_at, updated_at, product_name and
sku_name, all of which the table has and none of which anyone objected to. **A requirement
list enumerates what must exist, not what may exist.** REQ-FUL-002 therefore never
forbade sku_id. The captain's closing argument for keeping it is the snapshot one: these
rows already store product_name/sku_name as values, so carrying a line's names but not
the id they came from was the one inconsistent combination, and the inline FK to
product_skus is what stops the copy being able to disagree.

**Consequences now closed:**
- the two documentation contradictions I raised are void: REQ-FUL-002 is satisfied (it is
  a minimum), so neither it nor PHASE5_DESIGN §5.4 needs an edit for this column;
- docs/handoff/phase5-contract-contradictions.md **C2** is resolved in favour of keeping
  the column - read it as history, not as an open item. **C1** (REQ-PAY-001's
  payment_no UNIQUE reading against the merchant-scoped unique) is still worth a look but
  is a documentation question with no code impact.

**The failed-revert record, so nobody repeats it.** Four attempts, all abandoned and all
reverted via git checkout before any commit: regex deletion of column/FK/index/backfill;
then line-index deletion. Observed, in order: writes that reported success but did not
persist (file byte-identical to HEAD, git status clean); one write that landed and left an
IndentationError; then index arithmetic drifting on the 850-line migration and deleting the
unrelated ix_fulfillment_items_order_item_id as collateral. Verified after every attempt -
which is why none reached a commit. **If this ever genuinely must be reverted, use a NEW
revision (drop_index, drop_constraint(type_=foreignkey), drop_column) rather than
editing the applied revision in place, and have fulfilment revert its three call sites in the
same window.**

## 8. Known cross-suite coupling: `purge_shop` deletes by MERCHANT, not by test

Verified at the verifier's prompting. `purge_shop` resolves orders like this:

    order_ids = [ ... select(Order.id).where(Order.merchant_id == merchant_id) ... ]
    delete(Order).where(Order.id.in_(order_ids))

So it removes **every** order belonging to the fixture's merchant, not only the ones this
fixture created. That is deliberate - it is what lets a test that makes its own orders still
clean up - but it means any *other* test that extends the same shop inherits this teardown.
The verifier's FG-12 probes do exactly that: they call `paid_order(shop, ...)`, which creates
its order under that same merchant.

**Consequence, and it is the coupling the verifier was bitten by:** if their probe and my
suite run at the same time, my teardown can delete a probe order milliseconds after it was
created and committed. The probe then fails in a way that reads like a constraint defect
(e.g. a missing order or a refused write) rather than like a teardown race. This is the same
class as their measured `StaleDataError` / `ORDER_NOT_FOUND` findings: a shared mutable
resource, with the failure set moving between runs.

**Not fixed here, deliberately.** Any fix moves the boundary in one of two directions:
scoping the delete to only the ids this fixture recorded makes it correct but stops cleaning
orders a test created for itself; keeping the merchant scope requires each test to record its
own ids with the fixture, which is the verifier's file, not mine. Two safer options for
whoever picks this up:
1. give each concurrent consumer its own merchant (a fixture variant that does not share the
   shop), or
2. have the probe register its order in the fixture's `created` dict so teardown is exact.
Until then: **do not run the FG-12 probes concurrently with the commerce suite**, and treat a
probe failure during an active suite run as unverified rather than as evidence.
