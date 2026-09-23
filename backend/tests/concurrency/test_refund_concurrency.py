"""The refund concurrency probe: the last claimable amount cannot be refunded twice.

Lives in `tests/concurrency/` rather than beside the other FG-12 probes because it needs a
**blind except** in its worker thread: each thread records whatever went wrong and the test
asserts on the collected set afterwards. Letting one worker's exception escape would hide the
others, which is exactly the failure mode a concurrency test exists to detect - so the blind
catch is the mechanism, not sloppiness, and `tests/concurrency/**` is where the lint policy
allows it (`BLE001` in `backend/pyproject.toml`).

The invariant is the money, not the control flow: whatever the interleaving, the line is not
refunded twice and the counters stay consistent. The number of successful threads is
deliberately NOT pinned - the loser may legitimately be refused by the state guard (a fully
refunded claim becomes COMPLETED) or by a cap, and pinning which would assert an
implementation detail rather than the property.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.aftersales.workflow import RefundWorkflow
from app.shared.db.session import get_session_factory
from tests.integration.aftersales.conftest import (
    AfterSalesShop,
    _commerce_shop_instance as _commerce_shop_fixture,
    engine as _engine_fixture,
    read_money,
    seeded_shop as _seeded_shop_fixture,
)

#: Re-exported under their canonical names so pytest resolves them; see the sibling
#: integration module for why the whole chain is imported rather than the leaf alone.
_commerce_shop_instance = _commerce_shop_fixture
engine = _engine_fixture
seeded_shop = _seeded_shop_fixture

pytestmark = [pytest.mark.concurrency]


def _lines(session: Session, order_id: int) -> dict[int, tuple[int, int]]:
    """order_item_id -> (payable_amount, refunded_amount), read as SQL on a fresh session."""
    rows = session.execute(
        text(
            "SELECT id, payable_amount, refunded_amount FROM order_items "
            "WHERE order_id = :oid ORDER BY id"
        ),
        {"oid": order_id},
    ).all()
    return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}


def _approve(seeded_shop: AfterSalesShop, claim, amount: int) -> None:
    session = get_session_factory()()
    try:
        seeded_shop.approve(session, claim, amount=amount)
        session.commit()
    finally:
        session.close()


def _file(seeded_shop: AfterSalesShop, amount: int, items, suffix: str):
    pairs = list(items)
    if pairs and isinstance(pairs[0], tuple):
        item_indexes = tuple(index for index, _ in pairs)
        quantities = tuple(quantity for _, quantity in pairs)
    else:
        item_indexes = tuple(pairs)
        quantities = None
    session = get_session_factory()()
    try:
        return seeded_shop.file_claim(
            session, amount=amount, item_indexes=item_indexes, quantities=quantities, suffix=suffix
        )
    finally:
        session.close()

def test_concurrent_refunds_of_the_last_claimable_amount_settle_once(
    seeded_shop: AfterSalesShop,
) -> None:
    session = get_session_factory()()
    try:
        lines = _lines(session, seeded_shop.order_id)
    finally:
        session.close()

    ids = sorted(lines)
    target = ids[0]
    amount = lines[target][0]

    claim = _file(seeded_shop, amount, (0,), "race-1")
    _approve(seeded_shop, claim, amount)

    barrier = threading.Barrier(2, timeout=30)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(tag: str) -> None:
        session = get_session_factory()()
        try:
            barrier.wait()
            RefundWorkflow(session).execute(
                principal=seeded_shop.staff,
                after_sale_no=claim.after_sale_no,
                amount=amount,
                idempotency_key=seeded_shop.key(f"race-{tag}"),
            )
            session.commit()
            with lock:
                outcomes.append(f"applied:{tag}")
        except Exception as exc:
            session.rollback()
            with lock:
                outcomes.append(f"refused:{tag}:{type(exc).__name__}:{exc}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a worker did not finish"

    applied = [entry for entry in outcomes if entry.startswith("applied")]
    refused = [entry for entry in outcomes if entry.startswith("refused")]

    session = get_session_factory()()
    try:
        after = _lines(session, seeded_shop.order_id)
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        payable, refunded = after[target]

        assert applied, f"no thread succeeded; the claim was not refundable: {outcomes}"
        assert refunded == payable, (
            f"exactly one full refund should have landed: line {refunded} of {payable}; "
            f"outcomes={outcomes}"
        )
        assert money["order_refunded"] == amount, (
            f"the order moved {money['order_refunded']} for a single {amount} refund; "
            f"outcomes={outcomes}"
        )
        assert money["line_refunded_total"] == amount
        assert money["payment_refunded"] <= money["payment_paid"]
        assert money["payment_status"] == "PARTIAL_REFUNDED", money
    finally:
        session.close()

    for entry in refused:
        assert "OperationalError" not in entry, f"a raw database error escaped the workflow: {entry}"
        assert "IntegrityError" not in entry, f"a raw database error escaped the workflow: {entry}"
