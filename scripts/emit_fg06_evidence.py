"""Emit the FG-06 (Seed Idempotency) evidence artifact from two real seed runs.

The gate's frozen wording is "run the seed twice consecutively, compare the result
and the key data counts, and prove idempotency". This repository has no separate
demo-seed CLI: the only programmatic seeder is the shared commerce seed in
``tests/integration/payment/conftest.py`` (``_seed``/``_purge``), which is exactly
the loader ``tests/e2e/payment_browser_fixture.py`` already drives outside pytest.
So that is the seed under test, and the evidence says so rather than implying a
demo loader exists.

What is actually proven, and why those are the right things
-----------------------------------------------------------
Inside one real process the driver seeds, snapshots, purges, then seeds again:

  * the second run **succeeds** - it must not collide with the first run;
  * the second merchant-scoped shape is **identical** to the first (same number of
    products, SKUs, warehouses, stock rows, orders, lines, payments, fulfilments
    and outbox rows), so the loader is deterministic rather than whatever the
    database happened to contain;
  * the **global vocabulary is untouched** by the second run. ``permissions`` is a
    shared on-demand lookup table (``uq_permissions_code``). The fixture comments
    record the concrete failure this guards: a seed that deleted or re-inserted
    ``order:read`` made the *next* run fail with a duplicate-key or foreign-key
    error, so the breakage appeared in whatever ran second rather than in the code
    that caused it. Comparing the vocabulary count across the second seed is the
    direct test of that property;
  * each run **cleans up after itself**, so this gate does not itself become the
    residue that design section 13.5 exists to prevent.

Snapshots are taken *while each seeded merchant exists*, and deltas are compared
against a baseline taken before the first seed - absolute totals on a shared
database would prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from gate_evidence import ROOT, git_state

JSON_OUT = ROOT / "artifacts" / "evidence" / "migration" / "fg06_seed_idempotency.json"

#: The seed is a *test* fixture loader, so it is driven under the same frozen test
#: environment the pytest suite pins (tests/conftest.py). The database is the real
#: MySQL either way; these flags only stop the loader from reaching for a live LLM
#: or object store it does not need.
SEED_ENV = {
    "APP_ENV": "test",
    "APP_DEBUG": "false",
    "LOG_JSON": "false",
    "LOG_LEVEL": "WARNING",
    "AI_USE_FAKE_PROVIDERS": "true",
    "RATE_LIMIT_ENABLED": "false",
    "SEED_DETERMINISTIC": "true",
}

#: The driver runs inside the backend import path so it reuses the committed seed
#: verbatim. A copy of the seed here would be a second thing to keep in sync.
_DRIVER = r'''
import json, uuid
import sys
sys.path.insert(0, "backend")

from sqlalchemy import delete, func, select
from app.core.config import get_settings
from app.shared.db.session import configure_database, get_session_factory

get_settings.cache_clear()
configure_database(get_settings())

from tests.integration.payment.conftest import _purge, _seed
from app.modules.catalog.models import Product, ProductSku
from app.modules.fulfillment.models import Fulfillment
from app.modules.identity.models import Merchant, Permission, User
from app.modules.inventory.models import Inventory, Warehouse
from app.modules.order.models import Order, OrderItem
from app.modules.payment.models import Payment
from app.shared.db.models.idempotency import IdempotencyRecord
from app.shared.db.models.outbox import OutboxMessage


def _count(session, model, *clauses):
    stmt = select(func.count()).select_from(model)
    for clause in clauses:
        stmt = stmt.where(clause)
    return int(session.execute(stmt).scalar_one())


def snapshot(marker, merchant_id=None):
    with get_session_factory()() as session:
        data = {
            "merchants_total": _count(session, Merchant),
            "permissions_total": _count(session, Permission),
        }
        if merchant_id is not None:
            order_ids = select(Order.id).where(Order.merchant_id == merchant_id)
            data.update({
                "products": _count(session, Product, Product.merchant_id == merchant_id),
                "product_skus": _count(session, ProductSku, ProductSku.merchant_id == merchant_id),
                "warehouses": _count(session, Warehouse, Warehouse.merchant_id == merchant_id),
                "inventories": _count(session, Inventory, Inventory.merchant_id == merchant_id),
                "orders": _count(session, Order, Order.merchant_id == merchant_id),
                "order_items": _count(session, OrderItem, OrderItem.order_id.in_(order_ids)),
                "payments": _count(session, Payment, Payment.merchant_id == merchant_id),
                "fulfillments": _count(session, Fulfillment, Fulfillment.merchant_id == merchant_id),
                "outbox_messages": _count(session, OutboxMessage, OutboxMessage.merchant_id == merchant_id),
                "users_marker": _count(session, User, User.username.like(f"%{marker}%")),
            })
        return data


def cleanup(shop):
    with get_session_factory()() as session:
        session.execute(delete(IdempotencyRecord).where(
            ((IdempotencyRecord.resource_type == "ORDER") & (IdempotencyRecord.resource_id == shop.order_id))
            | ((IdempotencyRecord.resource_type == "PAYMENT") & (IdempotencyRecord.resource_id == shop.payment_id))))
        session.commit()
    _purge(shop)


result = {"baseline": snapshot(None), "runs": []}
for label in ("1", "2"):
    marker = uuid.uuid4().hex[:8].upper()
    shop = _seed(marker)
    scoped = snapshot(marker, shop.merchant_id)
    # Vocabulary is measured with the merchant present but before the next seed,
    # so any change is attributable to the seed that follows.
    result["runs"].append({"label": label, "marker": marker, "merchant_id": shop.merchant_id,
                           "scoped": scoped, "global_with_merchant": snapshot(None)})
    cleanup(shop)
    result["runs"][-1]["global_after_cleanup"] = snapshot(None)

print("SEED_JSON " + json.dumps(result))
'''


def main() -> int:
    started = datetime.now(UTC)
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    assertions: list[dict[str, object]] = []
    transcript: list[str] = []

    def check(name: str, passed: bool, actual: str) -> None:
        assertions.append({"name": name, "expected": "PASSED", "actual": actual, "pass": passed})

    env = dict(os.environ)
    env.update(SEED_ENV)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _DRIVER], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900, check=False, env=env,
        )
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, output = 124, "seed driver exceeded the 900s cap"
    transcript.append(f"# seed driver exit={code}\n{output}")

    result: dict | None = None
    for line in output.splitlines():
        if line.startswith("SEED_JSON "):
            try:
                result = json.loads(line[len("SEED_JSON "):])
            except ValueError:
                result = None

    check("the seed driver ran two consecutive seeds without error", code == 0 and result is not None,
          f"exit_code={code} parsed={'yes' if result is not None else 'no'}")

    if result and len(result.get("runs", [])) == 2:
        baseline = result["baseline"]
        first, second = result["runs"]
        s1, s2 = first["scoped"], second["scoped"]
        shape_keys = [k for k in s1 if not k.endswith("_total")]
        same_shape = all(s1[k] == s2[k] for k in shape_keys)
        check("both seeds produced an identical merchant-scoped data shape", same_shape,
              json.dumps({k: [s1[k], s2[k]] for k in shape_keys}))

        vocab_after_1 = first["global_after_cleanup"]["permissions_total"]
        vocab_after_2 = second["global_after_cleanup"]["permissions_total"]
        check("the second seed did not duplicate the global permission vocabulary",
              vocab_after_1 == vocab_after_2, f"after_run1={vocab_after_1} after_run2={vocab_after_2}")

        check("each seed created exactly one merchant",
              s1["merchants_total"] - baseline["merchants_total"] == 1
              and s2["merchants_total"] - baseline["merchants_total"] == 1,
              f"baseline={baseline['merchants_total']} run1={s1['merchants_total']} run2={s2['merchants_total']}")

        check("each seed cleaned up after itself (no merchant left behind)",
              first["global_after_cleanup"]["merchants_total"] == baseline["merchants_total"]
              and second["global_after_cleanup"]["merchants_total"] == baseline["merchants_total"],
              f"baseline={baseline['merchants_total']} "
              f"after1={first['global_after_cleanup']['merchants_total']} "
              f"after2={second['global_after_cleanup']['merchants_total']}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    source_state = git_state(
        (
            "backend/tests/integration/payment/conftest.py",
            "backend/tests/integration/commerce/seed.py",
            "backend/app",
            "backend/migrations",
            "scripts/emit_fg06_evidence.py",
            "scripts/gate_evidence.py",
        )
    )
    verdict = (
        "PASS"
        if assertions and all(item["pass"] is True for item in assertions)
        and source_state["relevant_paths_dirty"] is False
        else "FAIL"
    )

    report = {
        "gate_id": "FG-06",
        "name": "Seed Idempotency",
        "mandatory": False,
        "spec": "two consecutive seeds succeed, produce an identical shape, and do not duplicate the global permission vocabulary",
        "command": f"{sys.executable} scripts/emit_fg06_evidence.py",
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": 0 if verdict == "PASS" else 1,
        "environment": SEED_ENV,
        "seed_under_test": "tests/integration/payment/conftest.py::_seed (the only programmatic seeder in the repository)",
        "observations": result,
        "git": source_state,
        "assertions": assertions,
        "junit_report": None,
        "transcript_tail": "\n".join(transcript)[-4000:],
        "verdict": verdict,
    }
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {JSON_OUT.relative_to(ROOT)}")
    print(f"assertions={len(assertions)} verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']} -> {item['actual']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
