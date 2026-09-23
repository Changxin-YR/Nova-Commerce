"""Fixtures and read-back helpers for the Phase 6 outbox tests.

## Why this file re-exports the shared seed instead of re-deriving a world

There is exactly **one** definition of the Phase 5 world (``tests/integration/commerce/seed.py``)
and exactly one definition of a genuinely paid order (``seed.paid_order``, which drives
``CreateOrderWorkflow``, ``PaymentService.create`` and ``PaymentSuccessWorkflow`` with a real
signed callback). This package therefore *imports* ``engine`` and ``shop`` rather than
re-deriving them - the duplication ``PHASE5_DESIGN`` section 12 created that seed to prevent
would otherwise reappear here, and two shops can silently disagree about what "paid" means.

Importing them into a ``conftest.py`` is what makes them injectable in this package: pytest
registers a fixture under the name it was defined with and only within the requesting test's
conftest chain, and ``tests/integration/commerce/`` is a sibling, not an ancestor. The two
imported objects *are* the shared fixtures - re-exported, not reimplemented - so a test here
gets the same world (and the same ``purge_shop`` finalizer, including its ``outbox_messages``
sweep) as the commerce and after-sales suites.

## The after-sales chain, and why it is *driven* rather than redefined

The refund seam needs the after-sales suite's ``seeded_shop``: the shared world, plus a
genuinely paid order, plus the claim lifecycle, plus the staff role extended **in the
database**. pytest registers a fixture only under the name it was *defined* with and only
within the requesting test's conftest chain, so ``tests/integration/aftersales/conftest.py``
- a sibling - is not in this package's chain and ``seeded_shop`` is not injectable here.

Two options were open: copy the two fixtures, or drive the shared seed's own generator from a
fixture of this package's. The second is chosen, and it is what the after-sales conftest does
for the same reason: there must be exactly **one** definition of the Phase 5 shop, and two
definitions can silently disagree about what "paid" means. The forwarding below adds the one
thing the aftersales module's version adds - ``_extend_staff_role`` - so the claim helpers
work, and it passes this fixture's ``request`` through so the seed's ``addfinalizer`` cleanup
(including its ``outbox_messages`` sweep) still runs on every exit path, setup failure
included.

## Why the read-back goes through SQL

Every assertion about committed state is made against a **fresh session reading the rows**,
not against an ORM object a workflow's own ``commit()`` might have refreshed. ``HANDOFF``
section 6 names the failure mode: a read through the writing session can compare a snapshot
against itself and pass as a false green. ``outbox_row`` below is deliberately a query rather
than a property on the fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.order.models import OrderItem
from app.modules.order.workflow import OrderLineInput
from app.shared.db.models.outbox import OutboxMessage, OutboxStatus
from app.shared.db.session import get_session_factory
from tests.integration.aftersales.conftest import (
    AfterSalesShop,
    _extend_staff_role,
)
from tests.integration.commerce.seed import Shop, engine, paid_order, shop as shared_shop

#: The shared world, re-exported under the name every other suite injects it by.
shop = shared_shop

__all__ = [
    "AfterSalesShop",
    "Shop",
    "engine",
    "outbox_row",
    "outbox_rows",
    "row_counts",
    "seeded_shop",
    "shared_shop",
    "shop",
    "whole_second",
]


# ---------------------------------------------------------------------------
# The after-sales chain (see the module docstring for why it is driven)
# ---------------------------------------------------------------------------
@pytest.fixture
def _commerce_shop_instance(request: pytest.FixtureRequest, engine) -> Iterator[Shop]:
    """One instance of the shared seed's world for one test.

    ``shared_shop.__wrapped__`` is the undecorated fixture function: calling it keeps the
    requirement that matters - one definition of the shop, in the shared seed - while giving
    this package a fixture it can actually inject. The ``request`` is forwarded so
    ``purge_shop``'s ``addfinalizer`` registration lands on *this* test.
    """
    generator = shared_shop.__wrapped__(engine=engine, request=request)  # type: ignore[attr-defined]
    yield next(generator)


@pytest.fixture
def seeded_shop(_commerce_shop_instance: Shop) -> Iterator[AfterSalesShop]:
    """The shared world, a genuinely paid order, and the after-sales staff grants.

    The paid order is created with three units on one line (through the public
    ``OrderLineInput`` interface, so pricing and allocation still run) because a claim consumes
    *units*: with one unit per line a second claim on the same line is impossible.
    """
    _extend_staff_role(_commerce_shop_instance)

    order = paid_order(
        _commerce_shop_instance,
        lines=[OrderLineInput(sku_id=_commerce_shop_instance.sku_ids[0], quantity=3)],
        suffix="outbox-seam",
    )

    factory = get_session_factory()
    with factory() as session:
        items = list(
            session.execute(
                select(OrderItem)
                .where(OrderItem.order_id == order.id)
                .order_by(OrderItem.id.asc())
            )
            .scalars()
            .all()
        )
        assert items, "the paid order should have at least one line"

    yield AfterSalesShop(
        seeded=_commerce_shop_instance,
        order_no=order.order_no,
        order_id=int(order.id),
        order_item_ids=tuple(int(item.id) for item in items),
        order_item_amounts=tuple(int(item.payable_amount) for item in items),
    )


def outbox_rows(
    session: Session,
    *,
    merchant_id: int,
    aggregate_type: str | None = None,
    event_type: str | None = None,
) -> list[OutboxMessage]:
    """Every event row this merchant owns, oldest first, optionally narrowed.

    ``merchant_id`` is required rather than optional: it is the only attribution the row
    carries (there is no foreign key to the order/payment/refund it describes), and a
    helper that could be called without it would eventually be used to assert on another
    test's rows.
    """
    stmt = select(OutboxMessage).where(OutboxMessage.merchant_id == merchant_id)
    if aggregate_type is not None:
        stmt = stmt.where(OutboxMessage.aggregate_type == aggregate_type)
    if event_type is not None:
        stmt = stmt.where(OutboxMessage.event_type == event_type)
    return list(
        session.execute(stmt.order_by(OutboxMessage.id.asc())).scalars()
    )


def outbox_row(
    *,
    merchant_id: int,
    aggregate_type: str,
    aggregate_id: int,
) -> OutboxMessage | None:
    """The one row for ``(event_type-agnostic) aggregate``, read on a fresh session.

    Keyed on the aggregate triple's second and third parts because that is the unique
    index's own prefix: a test asking "was this event queued?" is asking the same
    question the database answers.
    """
    factory = get_session_factory()
    with factory() as session:
        return (
            session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.merchant_id == merchant_id,
                    OutboxMessage.aggregate_type == aggregate_type,
                    OutboxMessage.aggregate_id == aggregate_id,
                )
            )
            .scalars()
            .one_or_none()
        )


def rows_for_merchant(
    *,
    merchant_id: int,
    event_type: str | None = None,
    aggregate_type: str | None = None,
) -> list[OutboxMessage]:
    """:func:`outbox_rows` on a session this helper opens and closes itself.

    Provided so a post-commit assertion never has to write
    ``outbox_rows(get_session_factory()(), ...)`` - a call that leaks a connection when the
    assertion above it fails, and a leaked pooled connection in a 5-session-greedy suite is
    how a run turns into a timeout somewhere else entirely.
    """
    factory = get_session_factory()
    with factory() as session:
        rows = outbox_rows(
            session,
            merchant_id=merchant_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
        )
        # Detach so the objects stay readable after the session closes; the callers only
        # read scalar columns, and an expired instance would raise on attribute access.
        session.expunge_all()
        return rows


def row_counts(*, merchant_id: int) -> dict[str, int]:
    """Event count per ``event_type`` for one merchant. A missing type is absent, not 0."""
    factory = get_session_factory()
    with factory() as session:
        counts: dict[str, int] = {}
        for row in session.execute(
            select(OutboxMessage.event_type).where(OutboxMessage.merchant_id == merchant_id)
        ).scalars():
            counts[str(row)] = counts.get(str(row), 0) + 1
        return counts


def whole_second(moment: datetime | None = None) -> datetime:
    """A UTC timestamp truncated to the second, for use as the publisher's ``now``.

    Two reasons this is not fussiness. ``next_retry_at`` is a ``DATETIME(3)``, so a value
    written from a microsecond-precision ``now`` is stored **truncated** - "not due yet"
    and "due" then differ by less than a millisecond, and a test that asserts on the
    boundary would be measuring driver rounding rather than the retry rule. Truncating the
    clock used to *compute* the boundary keeps the comparison exact and the arithmetic
    readable.
    """
    base = (moment or datetime.now(UTC)).astimezone(UTC)
    return base.replace(microsecond=0)


def payload_of(row: OutboxMessage) -> dict[str, Any]:
    """The row's payload as a plain dict, whatever the JSON codec handed back."""
    return dict(row.payload)


#: Re-exported so a test can name the status without importing the model itself.
PENDING = OutboxStatus.PENDING.value
FAILED = OutboxStatus.FAILED.value
PUBLISHED = OutboxStatus.PUBLISHED.value
DEAD = OutboxStatus.DEAD.value


def backoff_moment(base: datetime, seconds: int) -> datetime:
    """``base + seconds`` - named so a test's expected backoff reads as the formula."""
    return base + timedelta(seconds=seconds)
