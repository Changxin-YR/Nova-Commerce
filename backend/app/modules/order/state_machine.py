"""The order status machine - a pure guard, with no session and no I/O.

Spec references:
    §30  the order lifecycle
    §31  **shipping never changes ``order_status``**, and the four status fields
         are never inferred from one another
    §36  every top-level transition appends exactly one ``order_status_logs`` row

Why this is its own module rather than a handful of ``if`` statements inside
``OrderService``: the transition table is the *only* place an order status may
move (see the comment in :mod:`app.modules.order.enums`), and a rule that lives in
one function is testable without a database, without a principal and without a
transaction. Every legal transition and every illegal one is proved in
``tests/unit/modules/order/test_state_machine.py`` - including the negative cases,
because a guard nobody has seen reject anything is a guard nobody has tested.

## Two decisions worth naming

**A self-transition is refused.** ``PENDING_PAYMENT -> PENDING_PAYMENT`` is not in
the table, so "cancel an already-cancelled order" cannot be expressed as a no-op
transition. If it were allowed, a double-submitted cancel would append a status log
row that says nothing happened - which is exactly the audit noise §36 exists to
prevent, and it hides the double submit instead of surfacing it.

**The error code is a property of the *operation*, not of the target status.** The
wire contract freezes two distinct codes for the two guarded endpoints
(``ORDER_NOT_CANCELLABLE 50010``, ``ORDER_NOT_CONFIRMABLE 50011``) and a third,
generic one (``ORDER_STATE_INVALID 50004``) for everything else. A cancel endpoint
must always answer 50010 regardless of which state the order was in, so the caller
passes the error class in. Baking "target == CANCELLED means 50010" into the table
here would be wrong the moment some other operation also wants to reach
``CANCELLED``, and it would put a wire concern in a pure module.
"""

from __future__ import annotations

from app.core.errors import AppError, OrderNotCancellableError, OrderNotConfirmableError
from app.modules.order.enums import ORDER_STATUS_TRANSITIONS, OrderStatus

__all__ = [
    "TERMINAL_ORDER_STATUSES",
    "OrderStateMachine",
    "allowed_targets",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "transition_to_cancelled",
    "transition_to_completed",
]


#: States with no outgoing edge. Derived from the table rather than re-listed, so
#: adding a transition cannot leave a stale "terminal" claim behind.
TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    status for status, targets in ORDER_STATUS_TRANSITIONS.items() if not targets
)


def _coerce(status: OrderStatus | str) -> OrderStatus | None:
    """Normalise an enum member or a raw stored ``VARCHAR`` to an enum member.

    Returns ``None`` for an unknown value rather than raising ``ValueError``: a
    row holding a status the vocabulary does not know is a data-integrity problem
    that must surface as a *refused transition*, not as an unhandled exception in
    the middle of a write path. Callers turn the ``None`` into a domain error.
    """
    if isinstance(status, OrderStatus):
        return status
    try:
        return OrderStatus(status)
    except ValueError:
        return None


def allowed_targets(status: OrderStatus | str) -> frozenset[OrderStatus]:
    """The legal next states, or the empty set for an unknown/terminal state.

    Exported because the console needs it to decide which actions to render, and a
    second hand-written copy of the table in the API layer is how a UI ends up
    offering a button the server will refuse.
    """
    current = _coerce(status)
    if current is None:
        return frozenset()
    return ORDER_STATUS_TRANSITIONS[current]


def can_transition(current: OrderStatus | str, target: OrderStatus | str) -> bool:
    """Whether ``current -> target`` is allowed. Never raises; never mutates."""
    source = _coerce(current)
    destination = _coerce(target)
    if source is None or destination is None:
        return False
    return destination in ORDER_STATUS_TRANSITIONS[source]


def is_terminal(status: OrderStatus | str) -> bool:
    """Whether the status has no outgoing edge (``CANCELLED``/``CLOSED``)."""
    current = _coerce(status)
    return current in TERMINAL_ORDER_STATUSES


def assert_transition(
    current: OrderStatus | str,
    target: OrderStatus | str,
    *,
    error: type[AppError] | None = None,
    reason: str | None = None,
) -> OrderStatus:
    """Guard a transition, returning the resolved target or raising.

    Returns the :class:`OrderStatus` (not the raw input) so a caller assigns the
    normalised value to the row. That is not a convenience: writing a raw string
    back is how a row ends up holding a value the vocabulary rejects, and the
    database's ``CHECK`` would then fail at flush time with a far worse message.

    ``error`` selects the wire code - see the module docstring. It defaults to
    :class:`~app.core.errors.OrderStateInvalidError` (50004).
    """
    from app.core.errors import OrderStateInvalidError

    error_cls: type[AppError] = error or OrderStateInvalidError

    source = _coerce(current)
    destination = _coerce(target)

    if source is None:
        raise error_cls(
            "the order is in an unrecognised state, so no transition can be authorised",
            context={"from_status": str(current), "to_status": str(target)},
        )
    if destination is None:
        raise error_cls(
            "the requested target state is not part of the order vocabulary",
            context={"from_status": source.value, "to_status": str(target)},
        )

    if destination not in ORDER_STATUS_TRANSITIONS[source]:
        raise error_cls(
            reason
            or f"order_status cannot move from {source.value} to {destination.value}",
            context={
                "from_status": source.value,
                "to_status": destination.value,
                # Handing the client the legal set means the UI can correct itself
                # instead of retrying the same refused call.
                "allowed": sorted(target.value for target in ORDER_STATUS_TRANSITIONS[source]),
            },
        )
    return destination


# ---------------------------------------------------------------------------
# Operation-level guards
#
# Two named entry points because the two frozen endpoints have their own codes.
# Keeping them here (rather than in the service) means the mapping "this
# operation answers with this code" is stated once and unit-tested without a
# database.
# ---------------------------------------------------------------------------
def transition_to_cancelled(current: OrderStatus | str) -> OrderStatus:
    """Guard ``POST /orders/{order_no}/cancel``. Always answers 50010 when refused."""
    return assert_transition(
        current,
        OrderStatus.CANCELLED,
        error=OrderNotCancellableError,
        reason=None,
    )


def transition_to_completed(current: OrderStatus | str) -> OrderStatus:
    """Guard ``POST /orders/{order_no}/confirm-receipt``. Always answers 50011."""
    return assert_transition(
        current,
        OrderStatus.COMPLETED,
        error=OrderNotConfirmableError,
    )


class OrderStateMachine:
    """Namespace for the guard, so call sites read as ``OrderStateMachine.can_...``.

    A class rather than free functions only because the service and the API layer
    both want a stable name to hang these off; every method is a ``staticmethod``
    and the class holds no state, which keeps it usable from a pure test and from
    inside the create transaction identically.
    """

    #: Terminal states, exposed for the console's "no actions available" branch.
    TERMINAL = TERMINAL_ORDER_STATUSES
    #: The frozen table, exposed read-only for tests and documentation.
    TRANSITIONS = ORDER_STATUS_TRANSITIONS

    @staticmethod
    def allowed_targets(status: OrderStatus | str) -> frozenset[OrderStatus]:
        return allowed_targets(status)

    @staticmethod
    def can_transition(current: OrderStatus | str, target: OrderStatus | str) -> bool:
        return can_transition(current, target)

    @staticmethod
    def is_terminal(status: OrderStatus | str) -> bool:
        return is_terminal(status)

    @staticmethod
    def assert_transition(
        current: OrderStatus | str,
        target: OrderStatus | str,
        *,
        error: type[AppError] | None = None,
        reason: str | None = None,
    ) -> OrderStatus:
        return assert_transition(current, target, error=error, reason=reason)

    @staticmethod
    def to_cancelled(current: OrderStatus | str) -> OrderStatus:
        return transition_to_cancelled(current)

    @staticmethod
    def to_completed(current: OrderStatus | str) -> OrderStatus:
        return transition_to_completed(current)
