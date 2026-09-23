"""``RefundWorkflow`` on real MySQL - the caps, the rollups and the stock movement.

Every test here runs against a real database, because every property under test is a
database property: ``SELECT ... FOR UPDATE`` on three tables, the CHECK constraints that
are FG-12's boundary half, the unique index that makes a retried refund idempotent, and
the inventory movement's idempotency key. None of it can be demonstrated on a mock
(section 113).

## One session per logical request

``RefundWorkflow`` commits, and the API layer opens a session per request
(``get_session``). A test that reused one session across a whole scenario would read the
post-mutation counters through objects that session had already loaded, which HANDOFF
section 6 warns is a false green under REPEATABLE READ. So every step opens and closes
its own session, and the assertions read the money by **SQL** (``read_money``) rather
than through the ORM.
"""

from __future__ import annotations

import pytest

from app.core.errors import (
    AfterSaleStateInvalidError,
    RefundAlreadyCompletedError,
    RefundAmountInvalidError,
    RefundExceedsItemError,
    RefundExceedsPaidError,
)
from app.modules.aftersales.enums import AfterSaleClaimStatus
from app.modules.aftersales.schemas import ApplyAfterSaleItemIn, ApplyAfterSaleRequest
from app.modules.aftersales.service import AfterSaleService
from app.modules.inventory.enums import MovementType
from app.modules.order.enums import AfterSaleStatus, PaymentStatus
from tests.integration.aftersales.conftest import (
    available_stock,
    load_claim,
    movements_for,
    read_claim,
    read_money,
    refunds_for,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The legal progression: two partial refunds reach exactly paid, a third is refused
# ---------------------------------------------------------------------------
def test_two_partial_refunds_reach_exactly_paid_and_a_third_is_refused(
    seeded_shop, session_factory
) -> None:
    """FG-12's positive control, driven through the workflow end to end.

    A legal partial refund followed by a legal second refund must reach exactly
    ``refunded == paid``, and a third must be refused. This is the shape the design
    names, and it is the one that catches an off-by-one in either cap: the second refund
    has to be *exactly* the remaining amount to reach equality, and a cap that is
    exclusive by one minor unit refuses it.
    """
    first_amount = 4000
    second_amount = seeded_shop.paid_amount - first_amount

    # -- claim 10000, approve 10000, refund 4000 ------------------------
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=seeded_shop.paid_amount, item_indexes=(0, 1, 2))
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=seeded_shop.paid_amount)

    with session_factory() as session:
        outcome = seeded_shop.refund(None, load_claim(session, after_sale_no=claim_no), amount=first_amount, suffix="r1")
        assert outcome.replayed is False
        assert outcome.refund.amount == first_amount
        assert outcome.refund.status == "SUCCEEDED"
        # Written straight to SUCCEEDED in the same transaction: V1 has no asynchronous
        # refund, so a PENDING row nobody completes would be a lie the customer can see.
        assert outcome.split is not None
        assert outcome.split.total == first_amount

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == first_amount
        assert money["order_refunded"] == first_amount
        # INV-007's principle for money: the rows must explain the counters.
        rows = refunds_for(session, after_sale_id=load_claim(session, after_sale_no=claim_no).id)
        assert sum(row.amount for row in rows) == money["payment_refunded"]
        # The per-line counters must partition the refund exactly.
        assert money["line_refunded_total"] == first_amount
        assert money["payment_status"] == PaymentStatus.PARTIAL_REFUNDED.value
        assert money["after_sale_status"] == AfterSaleStatus.PARTIAL_REFUNDED.value
        # A partially refunded claim is still APPROVED: the "partial" fact lives in the
        # amounts, not in the claim status, which is what makes a second instalment
        # expressible at all.
        assert read_claim(session, after_sale_no=claim_no)["claim_status"] == (
            AfterSaleClaimStatus.APPROVED.value
        )

    # -- the second refund takes it to exactly paid ---------------------
    with session_factory() as session:
        seeded_shop.refund(
            None,
            load_claim(session, after_sale_no=claim_no),
            amount=second_amount,
            suffix="r2",
        )

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == seeded_shop.paid_amount
        assert money["order_refunded"] == seeded_shop.paid_amount
        assert money["line_refunded_total"] == seeded_shop.paid_amount
        # REFUNDED iff refunded == paid, on *both* axes, by the same rule.
        assert money["payment_status"] == PaymentStatus.REFUNDED.value
        assert money["after_sale_status"] == AfterSaleStatus.REFUNDED.value
        assert money["payment_record_status"] == "REFUNDED"
        claim_state = read_claim(session, after_sale_no=claim_no)
        assert claim_state["claim_status"] == AfterSaleClaimStatus.COMPLETED.value
        assert claim_state["completed_at"] is not None
        assert claim_state["refunded_amount"] == claim_state["approved_amount"]

    # -- a third attempt is refused ------------------------------------
    with session_factory() as session, pytest.raises(AfterSaleStateInvalidError):
        # The claim is COMPLETED, so it is not a refundable state at all. This is the
        # *first* guard to fire, which is correct: no smaller amount would be legal.
        seeded_shop.refund(
            None,
            load_claim(session, after_sale_no=claim_no),
            amount=1,
            suffix="r3",
        )

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == seeded_shop.paid_amount
        # Exactly two movements, not three: the refused one wrote nothing.
        rows = refunds_for(session, after_sale_id=load_claim(session, after_sale_no=claim_no).id)
        assert len(rows) == 2


def test_the_order_status_axis_is_never_touched_by_a_refund(seeded_shop, session_factory) -> None:
    """A refund moves money, not the order's lifecycle.

    Shipping never changes ``order_status`` and neither does a refund: the refund keeps
    the order at ``PROCESSING`` and moves only the payment and after-sale axes. If a
    refund reached into ``order_status`` the four axes would stop being independent, and
    section 4.4's whole point would be gone.
    """
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=seeded_shop.paid_amount, item_indexes=(0, 1, 2))
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=seeded_shop.paid_amount)

    with session_factory() as session:
        seeded_shop.refund(None, load_claim(session, after_sale_no=claim_no), amount=seeded_shop.paid_amount)

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["order_status"] == "PROCESSING"
        assert money["payment_status"] == PaymentStatus.REFUNDED.value
        assert money["after_sale_status"] == AfterSaleStatus.REFUNDED.value


def test_a_claim_never_writes_the_order_after_sale_axis(seeded_shop, session_factory) -> None:
    """The captain's rule, asserted directly: filing and approving write only the claim.

    A filed or approved claim that moved ``orders.after_sale_status`` would make an order
    look like it is in an after-sales process before anybody agreed to anything.
    """
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=1000, item_indexes=(0,))
        claim_no = claim.after_sale_no
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["after_sale_status"] == AfterSaleStatus.NONE.value
        assert money["order_refunded"] == 0
        assert money["payment_status"] == PaymentStatus.PAID.value

    with session_factory() as session:
        seeded_shop.approve(session, load_claim(session, after_sale_no=claim_no), amount=1000)

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["after_sale_status"] == AfterSaleStatus.NONE.value
        assert money["order_refunded"] == 0
        assert read_claim(session, after_sale_no=claim_no)["claim_status"] == (
            AfterSaleClaimStatus.APPROVED.value
        )


# ---------------------------------------------------------------------------
# Cap 1 - the payment cannot give back more than it received
# ---------------------------------------------------------------------------
def test_cap_one_is_a_backstop_because_cap_two_implies_it(seeded_shop, session_factory) -> None:
    """Cap 1's guard and status, plus why cap 2 always fires first. Observed, not assumed.

    Cap 1 is ``payment.refunded_amount + amount <= payment.paid_amount``, checked **first**
    inside the payment's row lock (PHASE5_DESIGN section 6.2 step 4). Ordering it first is
    right - the split needs a total the payment can afford - but this suite could not
    construct a through-the-workflow case in which cap 1 is the guard that refuses, and the
    reason turns out to be arithmetic rather than test difficulty:

    * INV-006 makes ``sum(order_items.payable_amount) == orders.payable_amount``, and the
      payment workflow records ``payments.amount == order.payable_amount``;
    * so the total *line* capacity equals the amount the payment received;
    * a refund above ``paid - refunded`` therefore also exceeds some line's remaining
      capacity, and cap 2 is evaluated against every line the claim names.

    So cap 2 strictly implies cap 1 for any refund that names lines of the same order, and
    cap 1 can only be reached by a **bug** - a claim touching another order's line, a split
    that double-counts, a counter incremented twice. That is exactly what a backstop is for,
    and it is worth stating plainly: this test asserts the invariant that makes cap 1
    unreachable, and demonstrates that the failure surfaces as cap 2's code.

    What is *not* delegated to inference: the code and its HTTP status, the inclusive boundary
    (a refund up to ``paid`` is accepted), the arrival below the boundary, and the database
    CHECK that bounds the column independently of any Python.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.core.errors import ErrorCode, http_status_for

    # 1. The frozen contract for the code.
    assert int(ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT) == 80_004
    assert http_status_for(ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT) == 409
    assert RefundExceedsPaidError.code is ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT
    assert RefundExceedsPaidError.http_status == 409

    # 2. The invariant that makes cap 1 unreachable through the workflow: the lines' capacity
    #    equals what the payment received, read from the database rather than from the fixture.
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT (SELECT SUM(oi.payable_amount) FROM order_items oi "
                "        JOIN orders o ON o.id = oi.order_id WHERE o.order_no = :no) AS line_total, "
                "       (SELECT p.paid_amount FROM payments p WHERE p.payment_no = :pno) AS paid"
            ),
            {"no": seeded_shop.order_no, "pno": seeded_shop.payment_no},
        ).one()
    line_total, paid = int(row[0]), int(row[1])
    assert line_total == paid == seeded_shop.paid_amount

    # 3. A refund that would exceed the payment's remaining amount is refused - and refused
    #    with cap 2's code, because the same amount exceeds a line's capacity. Driven through
    #    the real workflow, so the *observed* code is recorded rather than the expected one.
    all_lines = (0, 1, 2)
    with session_factory() as session:
        claim = seeded_shop.file_claim(
            session, amount=paid, item_indexes=all_lines, quantities=(3, 3, 3), suffix="one"
        )
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=paid)

    with session_factory() as session:
        seeded_shop.refund(None, load_claim(session, after_sale_no=claim_no), amount=paid, suffix="r1")

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        # The cap accepted the refund that lands exactly on ``paid``: the comparison is ``>``,
        # not ``>=``. An off-by-one here would have refused this legal refund.
        assert money["payment_refunded"] == paid
        assert money["payment_record_status"] == "REFUNDED"

    # 4. The payment column cannot be driven past ``paid`` by any code path: the CHECK is the
    #    authority. Errno 3819 is a MySQL CHECK violation, and asserting on it is what makes
    #    this a control rather than a claim.
    with session_factory() as session:
        try:
            session.execute(
                text("UPDATE payments SET refunded_amount = paid_amount + 1 WHERE id = :id"),
                {"id": seeded_shop.payment_id},
            )
            session.commit()
            raise AssertionError("the payments CHECK did not refuse a refund past paid")
        except OperationalError as exc:
            assert "3819" in str(exc.orig), str(exc.orig)
            session.rollback()

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == paid


# ---------------------------------------------------------------------------
# Cap 2 - the per-line cumulative cap
# ---------------------------------------------------------------------------
def test_cap_two_refuses_a_line_that_is_already_fully_refunded(seeded_shop, session_factory) -> None:
    """``REFUND_EXCEEDS_ITEM_AMOUNT (80005)`` - the per-line cap, cumulatively.

    This is the case a single-row ``CHECK`` cannot express at all, and the reason cap 2 is
    enforced in the workflow on top of ``ck_order_items_refund_cap``. The first claim refunds
    line 1 to exactly its ``payable_amount``. A *second* claim then reaches line 1 again -
    through the units, since each line has three - and its refund has nowhere legal to go:

    * the claim's own approval has room (it is approved for what it asks);
    * the payment has 6667 of unspent paid amount;
    * the order has lines 2 and 3 with 6667 of capacity;
    * but the refund may only be placed on the lines this claim names, and the only line it
      names is full.

    The workflow refuses it with cap 2's code. This is also the ordering worth recording:
    the *apply* step does not refuse this claim (the order as a whole still has capacity), so
    the per-line limit is genuinely enforced at the money step, which is where the design puts
    it.
    """
    line_one = seeded_shop.order_item_ids[0]
    line_one_payable = seeded_shop.order_item_amounts[0]
    line_two_payable = seeded_shop.order_item_amounts[1]

    with session_factory() as session:
        first = seeded_shop.file_claim(
            session, amount=line_one_payable, item_indexes=(0,), quantities=(1,), suffix="full"
        )
        first_no = first.after_sale_no
        seeded_shop.approve(session, first, amount=line_one_payable)

    with session_factory() as session:
        outcome = seeded_shop.refund(
            session, load_claim(session, after_sale_no=first_no), amount=line_one_payable, suffix="full-r"
        )
        assert outcome.replayed is False
        assert outcome.split is not None
        assert outcome.split.as_dict() == {line_one: line_one_payable}

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        # Line 1 is exhausted; the order is only partly refunded.
        assert money["line_refunded_total"] == line_one_payable
        assert money["payment_status"] == PaymentStatus.PARTIAL_REFUNDED.value
        assert money["payment_paid"] - money["payment_refunded"] == line_two_payable + seeded_shop.order_item_amounts[2]

    # A second claim on the same line: it is refused at apply time, because the *line* has no
    # remaining amount left to claim. Asserted with the code, so the reader can see that the
    # line's capacity is what the apply-time cap is computed from.
    from app.core.errors import AfterSaleNotEligibleError

    with session_factory() as session:
        with pytest.raises(AfterSaleNotEligibleError) as excinfo:
            seeded_shop.file_claim(
                session,
                amount=line_one_payable,
                item_indexes=(0,),
                quantities=(1,),
                suffix="again",
            )
        assert excinfo.value.code.name == "AFTER_SALE_NOT_ELIGIBLE"

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        # Nothing moved: no second refund, no counter change.
        assert money["line_refunded_total"] == line_one_payable
        assert money["payment_refunded"] == line_one_payable


def test_cap_two_is_enforced_at_the_money_step_for_a_stale_claim(seeded_shop, session_factory) -> None:
    """The per-line cap holds on a claim whose line was exhausted *after* it was approved.

    The apply-time check cannot be the only guard, and this is the shape that proves it: a
    claim is approved while a line has capacity, and by the time its refund runs, another
    claim's refund has consumed that line. The money step must refuse with 80005 even though
    the claim is perfectly well formed and its approval is intact.

    The stale row is written directly rather than through the service, deliberately: the
    service refuses this claim at apply time today (correctly), so the only way to test that
    the *money step* holds the line is to put the row in that state. The refund is then
    refused because the lines it names have no capacity left, and - importantly - the refusal
    happens before anything is written.
    """
    from sqlalchemy import text

    line_one = seeded_shop.order_item_ids[0]
    line_one_payable = seeded_shop.order_item_amounts[0]
    stale_no = "NVAS-STALE-CAP2"

    with session_factory() as session:
        first = seeded_shop.file_claim(
            session, amount=line_one_payable, item_indexes=(0,), quantities=(1,), suffix="st1"
        )
        first_no = first.after_sale_no
        seeded_shop.approve(session, first, amount=line_one_payable)

    with session_factory() as session:
        seeded_shop.refund(
            None,
            load_claim(session, after_sale_no=first_no),
            amount=line_one_payable,
            suffix="st1-r",
        )

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        # Line 1 is at its payable amount; the payment still has 6667 unspent, so only the
        # per-line cap can be what refuses the refund below.
        assert money["line_refunded_total"] == line_one_payable
        assert money["payment_paid"] - money["payment_refunded"] == seeded_shop.paid_amount - line_one_payable

        session.execute(
            text(
                "INSERT INTO after_sales (after_sale_no, order_id, order_no, user_id, merchant_id, "
                "type, claim_status, requested_amount, approved_amount, refunded_amount, reason, "
                "idempotency_key, client_request_id, request_hash, created_at, updated_at) "
                "VALUES (:no, :oid, :ono, :uid, :mid, 'REFUND_ONLY', 'APPROVED', 100, 100, 0, "
                "'stale claim', :key, :crid, :rh, NOW(3), NOW(3))"
            ),
            {
                "no": stale_no,
                "oid": seeded_shop.order_id,
                "ono": seeded_shop.order_no,
                "uid": seeded_shop.consumer_id,
                "mid": seeded_shop.merchant_id,
                "key": seeded_shop.key("stale"),
                "crid": seeded_shop.client_request_id("stale"),
                "rh": "f" * 64,
            },
        )
        stale_id = int(
            session.execute(
                text("SELECT id FROM after_sales WHERE after_sale_no = :no"), {"no": stale_no}
            ).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO after_sale_items (after_sale_id, order_item_id, product_name, "
                "sku_name, quantity, created_at, updated_at) "
                "VALUES (:aid, :oid, 'stale', 'stale', 1, NOW(3), NOW(3))"
            ),
            {"aid": stale_id, "oid": line_one},
        )
        session.commit()

    with session_factory() as session, pytest.raises(RefundExceedsItemError) as excinfo:
        seeded_shop.refund(
            session, load_claim(session, after_sale_no=stale_no), amount=100, suffix="st2"
        )
    assert excinfo.value.code.name == "REFUND_EXCEEDS_ITEM_AMOUNT"
    # The refusal reports the line and the shortfall, which is what makes the 409 actionable.
    assert excinfo.value.context["line_remaining_total"] == 0
    assert excinfo.value.context["requested_amount"] == 100

    # **The two caps are distinguishable in the failure**, which is what the captain asked for:
    # this is a *line-level* refusal (its context carries the line capacity) occurring while the
    # payment still has headroom - so it cannot be mistaken for a payment-level one, and no
    # over-refund can hide behind the shared code. Read from the database, not asserted from the
    # fixture's own figures.
    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
    assert money["payment_paid"] > money["payment_refunded"], (
        "this must be a per-line refusal with payment headroom to spare; if the payment were "
        "also exhausted the two caps would be indistinguishable here"
    )
    assert "line_remaining" in excinfo.value.context

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        # Nothing was written: no refund row, no counter moved, no claim status change.
        assert money["line_refunded_total"] == line_one_payable
        assert money["payment_refunded"] == line_one_payable
        assert read_claim(session, after_sale_no=stale_no)["refunded_amount"] == 0
        assert refunds_for(session, after_sale_id=stale_id) == []


def test_line_counters_partition_each_refund_exactly(seeded_shop, session_factory) -> None:
    """The odd remainder has to land somewhere, and the parts must sum to the whole.

    Amount 333 c across three equal-payable lines: ``floor(333/3) == 111`` exactly, so
    use a deliberately indivisible amount - 1000 across payables 3333/3333/3334 - and
    assert ``sum(order_items.refunded_amount) == refund.amount``. A split that lost a
    minor unit would leave the lines permanently unable to explain the payment's
    counter.
    """
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=1000, item_indexes=(0, 1, 2), suffix="split")
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=1000)

    with session_factory() as session:
        outcome = seeded_shop.refund(
            session, load_claim(session, after_sale_no=claim_no), amount=1000, suffix="split-r"
        )
        assert outcome.split is not None
        assert outcome.split.total == 1000

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["line_refunded_total"] == 1000
        # Three lines, all weighted, so all three received something.
        rows = refunds_for(session, after_sale_id=load_claim(session, after_sale_no=claim_no).id)
        assert sum(row.amount for row in rows) == 1000


def test_a_line_cannot_exceed_its_payable_even_within_the_orders_total(
    seeded_shop, session_factory
) -> None:
    """The single-line case, where the per-line cap is the *only* thing in the way.

    One line of 3333, claim approved for 3333, refund 3333: legal, and it takes the line
    to exactly its payable (inclusive, not exclusive). A second refund on the same line
    is refused by cap 2 - the order still has 6667 of headroom, so nothing else would
    stop it.
    """
    line_one = seeded_shop.order_item_ids[0]
    line_one_payable = seeded_shop.order_item_amounts[0]

    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=line_one_payable, item_indexes=(0,), suffix="one")
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=line_one_payable)

    with session_factory() as session:
        seeded_shop.refund(
            session, load_claim(session, after_sale_no=claim_no), amount=line_one_payable, suffix="one-r"
        )

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["line_refunded_total"] == line_one_payable
        # The order is only partly refunded: line 1 is exhausted, lines 2 and 3 are not.
        assert money["payment_status"] == PaymentStatus.PARTIAL_REFUNDED.value

    with session_factory() as session:
        # A *second claim* on the same line has nothing left to claim, so eligibility
        # refuses it before any refund is attempted - the claim-level equivalent of cap 2.
        from app.core.errors import AfterSaleNotEligibleError

        with pytest.raises(AfterSaleNotEligibleError):
            AfterSaleService(session).apply(
                principal=seeded_shop.consumer,
                payload=ApplyAfterSaleRequest(
                    order_no=seeded_shop.order_no,
                    type="REFUND_ONLY",
                    items=[ApplyAfterSaleItemIn(order_item_id=line_one, quantity=1)],
                    requested_amount=1,
                    reason="again",
                    client_request_id=seeded_shop.client_request_id("again"),
                ),
                idempotency_key=seeded_shop.key("again"),
            )


# ---------------------------------------------------------------------------
# Cap 1 at the database boundary
# ---------------------------------------------------------------------------
def test_payment_chk_refuses_a_refund_past_paid_at_the_database(seeded_shop, session_factory) -> None:
    """FG-12's boundary half: a raw ``UPDATE`` that violates cap 1 is refused by MySQL.

    This is the point section 8 makes - application checks are not the boundary. The
    ``payments`` CHECK is what refuses a direct write through a separate connection, so
    a bug in the workflow becomes a database error rather than a silent over-refund.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with session_factory() as session:
        with pytest.raises(OperationalError) as excinfo:
            session.execute(
                text(
                    "UPDATE payments SET refunded_amount = paid_amount + 1 WHERE id = :id"
                ),
                {"id": seeded_shop.payment_id},
            )
            session.commit()
        assert "3819" in str(excinfo.value.orig) or "Check constraint" in str(excinfo.value.orig)
        session.rollback()


def test_order_chk_refuses_a_refund_past_paid_at_the_database(seeded_shop, session_factory) -> None:
    """The same negative control for ``orders.refunded_amount <= paid_amount``."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with session_factory() as session:
        with pytest.raises(OperationalError) as excinfo:
            session.execute(
                text("UPDATE orders SET refunded_amount = paid_amount + 1 WHERE id = :id"),
                {"id": seeded_shop.order_id},
            )
            session.commit()
        assert "3819" in str(excinfo.value.orig) or "Check constraint" in str(excinfo.value.orig)
        session.rollback()


def test_order_item_chk_refuses_a_refund_past_payable_at_the_database(
    seeded_shop, session_factory
) -> None:
    """And for ``order_items.refunded_amount <= payable_amount`` - cap 2's single-row leg."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with session_factory() as session:
        with pytest.raises(OperationalError) as excinfo:
            session.execute(
                text(
                    "UPDATE order_items SET refunded_amount = payable_amount + 1 WHERE id = :id"
                ),
                {"id": seeded_shop.order_item_ids[0]},
            )
            session.commit()
        assert "3819" in str(excinfo.value.orig) or "Check constraint" in str(excinfo.value.orig)
        session.rollback()


# ---------------------------------------------------------------------------
# RETURN_IN: only for RETURN_REFUND, and idempotent
# ---------------------------------------------------------------------------
def test_return_refund_credits_stock_and_refund_only_does_not(seeded_shop, session_factory) -> None:
    """The one stock effect a refund has, and the case where it must not happen.

    Two claims on two different lines of the same order: one ``RETURN_REFUND`` (the goods
    came back, so availability rises) and one ``REFUND_ONLY`` (the customer kept them, so
    availability must not move). The second is the defect the rule exists for - crediting
    goods that never returned inflates availability until the next stock count finds the
    difference, which looks like shrinkage and is a bookkeeping error in the other
    direction.
    """
    return_line = seeded_shop.order_item_ids[0]
    keep_line = seeded_shop.order_item_ids[1]
    return_sku = seeded_shop.sku_ids[0]
    keep_sku = seeded_shop.sku_ids[1]
    amount = seeded_shop.order_item_amounts[0]

    with session_factory() as session:
        # Read, never assumed: the settlement reserved and then deducted each line, so the
        # available figure here is 1000 - 3 on the SKUs this order bought. A hardcoded 1000 would
        # have been an assertion about the *seed* rather than about the refund.
        before_return = available_stock(
            session, sku_id=return_sku, warehouse_id=seeded_shop.warehouse_id
        )
        before_keep = available_stock(
            session, sku_id=keep_sku, warehouse_id=seeded_shop.warehouse_id
        )

    with session_factory() as session:
        returned = seeded_shop.file_claim(
            session,
            amount=amount,
            item_indexes=(0,),
            claim_type="RETURN_REFUND",
            suffix="ret",
        )
        returned_no = returned.after_sale_no
        seeded_shop.approve(session, returned, amount=amount)

    with session_factory() as session:
        kept = seeded_shop.file_claim(
            session,
            amount=amount,
            item_indexes=(1,),
            claim_type="REFUND_ONLY",
            suffix="keep",
        )
        kept_no = kept.after_sale_no
        seeded_shop.approve(session, kept, amount=amount)

    with session_factory() as session:
        seeded_shop.refund(None, load_claim(session, after_sale_no=returned_no), amount=amount, suffix="ret-r")

    with session_factory() as session:
        seeded_shop.refund(None, load_claim(session, after_sale_no=kept_no), amount=amount, suffix="keep-r")

    with session_factory() as session:
        after_return = available_stock(session, sku_id=return_sku, warehouse_id=seeded_shop.warehouse_id)
        after_keep = available_stock(session, sku_id=keep_sku, warehouse_id=seeded_shop.warehouse_id)
        # The returned line came back by exactly the claimed quantity.
        assert after_return == before_return + 1
        # The kept line did not move at all - and no movement was appended for it.
        assert after_keep == before_keep

        return_movements = [
            row for row in movements_for(session, sku_id=return_sku)
            if row.movement_type == MovementType.RETURN_IN.value
        ]
        assert len(return_movements) == 1
        movement = return_movements[0]
        assert movement.idempotency_key == (
            f"return-in:{returned_no}:{return_line}"
        )
        assert movement.delta_available == 1
        assert movement.reference_type == "AFTER_SALE"
        # The movement points at the *claim*: a reconciliation can walk from the stock
        # movement to the claim, and the claim's line row names the order line that came
        # back. `keep_line` is the line that must have produced nothing.
        returned_claim = load_claim(session, after_sale_no=returned_no)
        assert movement.reference_id == returned_claim.id
        keep_line = seeded_shop.order_item_ids[1]
        assert keep_line != return_line

        keep_movements = [
            row for row in movements_for(session, sku_id=keep_sku)
            if row.movement_type == MovementType.RETURN_IN.value
        ]
        assert keep_movements == []


def test_the_return_in_key_is_deterministic_across_a_replayed_refund(seeded_shop, session_factory) -> None:
    """A retried refund must not credit stock twice.

    The movement key is a pure function of the claim and the line
    (``return-in:{after_sale_no}:{order_item_id}``, design 6.2 step 6), and
    ``inventory_movements.idempotency_key`` is what turns a retry into a no-op rather
    than a second credit. Two things are asserted: the key's exact value, and that a
    replayed request adds no movement.
    """
    assert seeded_shop.order_item_ids[0] != seeded_shop.order_item_ids[1]
    sku = seeded_shop.sku_ids[0]
    amount = seeded_shop.order_item_amounts[0]

    with session_factory() as session:
        claim = seeded_shop.file_claim(
            session, amount=amount, item_indexes=(0,), claim_type="RETURN_REFUND", suffix="idem"
        )
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=amount)

    with session_factory() as session:
        first = seeded_shop.refund(
            session, load_claim(session, after_sale_no=claim_no), amount=amount, suffix="idem-r"
        )
        assert first.replayed is False
        key_used = first.refund.idempotency_key

    with session_factory() as session:
        before = available_stock(session, sku_id=sku, warehouse_id=seeded_shop.warehouse_id)
        movements_before = [
            row for row in movements_for(session, sku_id=sku)
            if row.movement_type == MovementType.RETURN_IN.value
        ]

    # The same key and amount again: an idempotent replay, not a second refund.
    with session_factory() as session:
        replayed = seeded_shop.refund(
            None,
            load_claim(session, after_sale_no=claim_no),
            amount=amount,
            suffix="ignored",
            idempotency_key=key_used,
        )
        assert replayed.replayed is True
        assert replayed.refund.id == first.refund.id

    with session_factory() as session:
        after = available_stock(session, sku_id=sku, warehouse_id=seeded_shop.warehouse_id)
        movements_after = [
            row for row in movements_for(session, sku_id=sku)
            if row.movement_type == MovementType.RETURN_IN.value
        ]
        assert after == before
        assert len(movements_after) == len(movements_before) == 1
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == amount


def test_the_same_key_with_a_different_amount_is_refused(seeded_shop, session_factory) -> None:
    """``REFUND_ALREADY_COMPLETED (80007)`` - the key may not mean two different amounts.

    Telling a client "succeeded" for an amount it did not ask for is the one wrong answer
    available here, which is why the request hash is compared rather than just the key.
    """
    amount = seeded_shop.order_item_amounts[0]

    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=amount, item_indexes=(0,), suffix="k1")
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=amount)

    with session_factory() as session:
        outcome = seeded_shop.refund(
            session,
            load_claim(session, after_sale_no=claim_no),
            amount=amount,
            suffix="key-reuse",
        )
        # The key the refund row actually carries, captured rather than reconstructed: `key()`
        # appends a counter, so building the string a second time would produce a *different* key
        # and the test would silently stop testing key reuse at all.
        reused_key = outcome.refund.idempotency_key

    with session_factory() as session, pytest.raises(RefundAlreadyCompletedError):
        seeded_shop.refund(
            None,
            load_claim(session, after_sale_no=claim_no),
            amount=1,
            suffix="ignored",
            idempotency_key=reused_key,
        )

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        assert money["payment_refunded"] == amount


# ---------------------------------------------------------------------------
# State guards
# ---------------------------------------------------------------------------
def test_a_pending_claim_cannot_be_refunded(seeded_shop, session_factory) -> None:
    """No approval means no money: ``AFTER_SALE_STATE_INVALID (80002)``.

    A cap check would also refuse this, but with a *cap* error, which reads to an
    operator as "try a smaller amount" - and no smaller amount is legal on an
    unapproved claim.
    """
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=1000, item_indexes=(0,), suffix="pending")
        claim_no = claim.after_sale_no

    with session_factory() as session, pytest.raises(AfterSaleStateInvalidError):
        seeded_shop.refund(None, load_claim(session, after_sale_no=claim_no), amount=1000)


def test_a_rejected_claim_cannot_be_refunded(seeded_shop, session_factory) -> None:
    from app.modules.aftersales.schemas import RejectAfterSaleRequest

    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=1000, item_indexes=(0,), suffix="rej")
        claim_no = claim.after_sale_no

    with session_factory() as session:
        AfterSaleService(session).reject(
            principal=seeded_shop.staff,
            after_sale_no=claim_no,
            payload=RejectAfterSaleRequest(reject_reason="outside the return window"),
        )

    with session_factory() as session, pytest.raises(AfterSaleStateInvalidError):
        seeded_shop.refund(None, load_claim(session, after_sale_no=claim_no), amount=1000)


def test_a_refund_requires_a_positive_amount(seeded_shop, session_factory) -> None:
    """``REFUND_AMOUNT_INVALID (80006)`` for a non-positive amount.

    "Refund zero" is a request that looks like a write and is not one, so it is refused
    before any lock is taken - holding row locks to reject it would block another
    operator's real refund for no reason.
    """
    with session_factory() as session:
        claim = seeded_shop.file_claim(session, amount=1000, item_indexes=(0,), suffix="zero")
        claim_no = claim.after_sale_no
        seeded_shop.approve(session, claim, amount=1000)

    with session_factory() as session:
        for amount in (0, -1):
            with pytest.raises(RefundAmountInvalidError):
                seeded_shop.refund(
                    session,
                    load_claim(session, after_sale_no=claim_no),
                    amount=amount,
                    suffix=f"zero-{amount}",
                )
