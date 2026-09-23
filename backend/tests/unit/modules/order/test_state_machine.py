"""The order status machine - every edge, and every refusal.

Spec §30/§31/§36, PHASE4_DESIGN §3, API_CONTRACT §14.5.

These are unit tests: the guard is pure, so it is exercised with no session, no
principal and no transaction. That is the point of keeping it in its own module -
a rule that needs a database to test is a rule that gets tested less.

Three things are asserted, and the third is the one that is usually skipped:

1. **Every legal edge** in the frozen table is permitted and returns the resolved
   enum member (not the raw string it was given).
2. **A representative set of illegal edges** is refused, including self-transitions
   and anything out of a terminal state.
3. **Which error code** each refusal carries. The wire contract freezes two separate
   codes for the two guarded endpoints (50010/50011), and a cancel that answered
   50004 for one state and 50010 for another would be a client-visible bug that a
   "did it raise?" assertion cannot detect.
"""

from __future__ import annotations

import pytest

from app.core.errors import (
    OrderNotCancellableError,
    OrderNotConfirmableError,
    OrderStateInvalidError,
)
from app.modules.order.enums import ORDER_STATUS_TRANSITIONS, OrderStatus
from app.modules.order.state_machine import (
    TERMINAL_ORDER_STATUSES,
    OrderStateMachine,
    allowed_targets,
    assert_transition,
    can_transition,
    is_terminal,
    transition_to_cancelled,
    transition_to_completed,
)

#: The frozen table, restated here as a literal so the test fails if the table is
#: edited rather than the code merely agreeing with itself. A test that derives its
#: expectations from the implementation proves nothing about the implementation.
EXPECTED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: {
        OrderStatus.PROCESSING,
        OrderStatus.CANCELLED,
        OrderStatus.CLOSED,
    },
    OrderStatus.PROCESSING: {OrderStatus.COMPLETED, OrderStatus.CLOSED},
    OrderStatus.COMPLETED: {OrderStatus.CLOSED},
    OrderStatus.CANCELLED: set(),
    OrderStatus.CLOSED: set(),
}


def _all_pairs():
    for source in OrderStatus:
        for target in OrderStatus:
            yield source, target


# ---------------------------------------------------------------------------
# 1. The table itself
# ---------------------------------------------------------------------------
def test_transition_table_matches_the_frozen_contract() -> None:
    assert set(ORDER_STATUS_TRANSITIONS) == set(OrderStatus)
    for source, expected in EXPECTED_TRANSITIONS.items():
        assert set(ORDER_STATUS_TRANSITIONS[source]) == expected, source


def test_every_legal_edge_is_permitted() -> None:
    for source, targets in EXPECTED_TRANSITIONS.items():
        for target in targets:
            assert can_transition(source, target), f"{source} -> {target}"
            assert assert_transition(source, target) is target


def test_every_illegal_edge_is_refused() -> None:
    refused = 0
    for source, target in _all_pairs():
        if target in EXPECTED_TRANSITIONS[source]:
            continue
        refused += 1
        assert not can_transition(source, target), f"{source} -> {target} should be refused"
        with pytest.raises(OrderStateInvalidError):
            assert_transition(source, target)
    # 25 ordered pairs minus the 6 legal edges.
    assert refused == 19


def test_a_self_transition_is_not_allowed() -> None:
    # PENDING_PAYMENT -> PENDING_PAYMENT would append a status log row saying
    # nothing happened, which hides a double submit instead of surfacing it.
    for status in OrderStatus:
        assert not can_transition(status, status)
        with pytest.raises(OrderStateInvalidError):
            assert_transition(status, status)


def test_cancelled_and_closed_are_terminal() -> None:
    assert {OrderStatus.CANCELLED, OrderStatus.CLOSED} == TERMINAL_ORDER_STATUSES
    for status in OrderStatus:
        expected = status in TERMINAL_ORDER_STATUSES
        assert is_terminal(status) is expected
        assert (allowed_targets(status) == frozenset()) is expected


def test_allowed_targets_is_exposed_for_the_console() -> None:
    # The UI needs the legal set to decide which actions to render; a second
    # hand-written copy in the frontend is how a button appears for a call the
    # server will refuse.
    assert allowed_targets(OrderStatus.PENDING_PAYMENT) == frozenset(
        {OrderStatus.PROCESSING, OrderStatus.CANCELLED, OrderStatus.CLOSED}
    )


# ---------------------------------------------------------------------------
# 2. Refusal payloads
# ---------------------------------------------------------------------------
def test_a_refusal_names_both_states_and_the_legal_set() -> None:
    with pytest.raises(OrderStateInvalidError) as caught:
        assert_transition(OrderStatus.CANCELLED, OrderStatus.COMPLETED)
    assert caught.value.context["from_status"] == "CANCELLED"
    assert caught.value.context["to_status"] == "COMPLETED"
    assert caught.value.context["allowed"] == []


def test_a_raw_string_from_the_database_is_accepted() -> None:
    """``order_status`` is a VARCHAR, so a row read yields a plain string."""
    assert assert_transition("PENDING_PAYMENT", "CANCELLED") is OrderStatus.CANCELLED
    assert can_transition("PROCESSING", "COMPLETED")


def test_the_guard_returns_the_enum_not_the_input() -> None:
    """A caller assigns this back to the row; writing a raw string risks a CHECK
    failure at flush time with a much worse message."""
    resolved = assert_transition("PENDING_PAYMENT", "PROCESSING")
    assert isinstance(resolved, OrderStatus)


def test_an_unrecognised_status_is_refused_rather_than_raising_valueerror() -> None:
    # A row holding a value outside the vocabulary is data corruption; it must
    # surface as a refused transition, not as a ValueError mid-write.
    assert not can_transition("SHIPPED", OrderStatus.CANCELLED)
    assert not is_terminal("SHIPPED")
    with pytest.raises(OrderStateInvalidError) as caught:
        assert_transition("SHIPPED", OrderStatus.CANCELLED)
    assert caught.value.context["from_status"] == "SHIPPED"


def test_an_unknown_target_is_refused() -> None:
    with pytest.raises(OrderStateInvalidError) as caught:
        assert_transition(OrderStatus.PENDING_PAYMENT, "SHIPPED")
    assert caught.value.context["to_status"] == "SHIPPED"


# ---------------------------------------------------------------------------
# 3. Operation-level codes (§14.3, §14.5)
# ---------------------------------------------------------------------------
def test_cancel_always_answers_50010() -> None:
    from app.core.errors import ErrorCode

    # The legal case works.
    assert transition_to_cancelled(OrderStatus.PENDING_PAYMENT) is OrderStatus.CANCELLED

    # Every illegal case answers 50010, whatever the order's state was - a cancel
    # endpoint that answered 50004 for one state and 50010 for another would break
    # the client's error mapping.
    for source in (OrderStatus.PROCESSING, OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.CLOSED):
        with pytest.raises(OrderNotCancellableError) as caught:
            transition_to_cancelled(source)
        assert int(caught.value.code) == int(ErrorCode.ORDER_NOT_CANCELLABLE) == 50010
        assert caught.value.status_code == 409


def test_confirm_receipt_always_answers_50011() -> None:
    from app.core.errors import ErrorCode

    assert transition_to_completed(OrderStatus.PROCESSING) is OrderStatus.COMPLETED

    for source in (
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.COMPLETED,
        OrderStatus.CANCELLED,
        OrderStatus.CLOSED,
    ):
        with pytest.raises(OrderNotConfirmableError) as caught:
            transition_to_completed(source)
        assert int(caught.value.code) == int(ErrorCode.ORDER_NOT_CONFIRMABLE) == 50011
        assert caught.value.status_code == 409


def test_the_two_guarded_codes_are_distinct() -> None:
    """The client branches on these, so they must not collapse."""
    assert int(OrderNotCancellableError.code) != int(OrderNotConfirmableError.code)
    assert int(OrderNotCancellableError.code) != int(OrderStateInvalidError.code)


# ---------------------------------------------------------------------------
# 4. The namespace wrapper
# ---------------------------------------------------------------------------
def test_the_namespace_exposes_the_same_behaviour() -> None:
    assert OrderStateMachine.can_transition("PROCESSING", "COMPLETED")
    assert OrderStateMachine.to_completed("PROCESSING") is OrderStatus.COMPLETED
    assert OrderStateMachine.to_cancelled("PENDING_PAYMENT") is OrderStatus.CANCELLED
    assert OrderStateMachine.TRANSITIONS is ORDER_STATUS_TRANSITIONS
    assert OrderStateMachine.TERMINAL == TERMINAL_ORDER_STATUSES


# ---------------------------------------------------------------------------
# 5. §31: shipping never moves order_status
# ---------------------------------------------------------------------------
def test_fulfillment_statuses_are_not_inputs_to_the_guard() -> None:
    """A fulfillment status is not an order status, and must not be accepted as one.

    §31's separation is only real if the guard physically cannot be handed a
    ``SHIPPED``/``DELIVERED`` value and act on it.
    """
    from app.modules.order.enums import FulfillmentStatus

    for status in FulfillmentStatus:
        assert not can_transition(status.value, OrderStatus.COMPLETED)
        assert not can_transition(OrderStatus.PROCESSING, status.value)
