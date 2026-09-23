"""The three marked outbox seams, driven through the **real** workflows (§49).

    order.created      CreateOrderWorkflow     step 9
    payment.settled    PaymentSuccessWorkflow  step 10
    refund.succeeded   RefundWorkflow          step 7

## Why these tests go through the workflows rather than calling ``enqueue``

``test_writer.py`` proves the writer works. It cannot prove the *seam* works, and the seam is
where the interesting properties live:

* the row is written **inside** the business transaction - so a rollback takes it with it
  (``test_writer.py``) and one commit publishes both together;
* the ``idempotency_key`` recorded is the emitter's own key, which is what an operator greps
  during an incident;
* the payload's keys are the contract. A consumer routes on them, so a renamed key is a
  breaking change that no type checker sees;
* the *position* of the seam block is load-bearing: above the guards, a duplicate callback or
  a replayed refund would emit a second event.

Every expected value below is therefore read back from the **business row the workflow
wrote** (``orders.order_no``, ``payments.payment_no``, ``refunds.refund_no``) rather than
restated as a literal. A literal would keep passing if the workflow wrote a different row
than the event claimed.

## Fixtures come from the shared seed, not from here

``shop`` is ``tests/integration/commerce/seed.py``'s world (re-exported by this package's
conftest), ``paid_order`` is its genuinely-settled-order helper, and for the refund seam
``seeded_shop`` is the after-sales suite's ``AfterSalesShop`` - the world plus a paid order
plus the claim lifecycle, with the staff role extended **in the database** so
``RefundWorkflow`` runs against a state a real request could have produced. Nothing here
re-seeds a merchant, and nothing forges a ``Principal`` whose permissions were assembled in
Python.
"""

# ruff: noqa: F811 - the imported fixture names are the test parameters' names.

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.redaction import assertion_that_no_secret_remains
from app.modules.order.models import Order
from app.modules.order.workflow import OrderLineInput
from app.modules.payment.models import Payment
from app.modules.payment.workflow import PaymentSuccessWorkflow
from app.shared.db.session import get_session_factory
from app.shared.outbox import OutboxAggregateType, OutboxEventType
from tests.integration.aftersales.conftest import load_claim
from tests.integration.commerce.seed import (
    Shop as SeededShop,
    make_order,
    settle_order,
    signed_success_callback,
)

# The fixtures are imported so pytest registers them for *this* package (a sibling conftest is
# not in the requesting test's chain) and the test functions take them as parameters under the
# same name - which ruff reads as a redefinition, hence the per-file F811 ignore in the header.
from tests.integration.outbox.conftest import (  # noqa: F401
    AfterSalesShop,
    rows_for_merchant,
    seeded_shop,
)

pytestmark = pytest.mark.integration

#: The exact payload contract of ``refund.succeeded`` - the ten keys ``RefundWorkflow``'s
#: step-7 seam comment names. Restated here **on purpose**: this is a contract assertion, and
#: a contract that read its own source back would agree with any edit to it.
REFUND_PAYLOAD_KEYS = frozenset(
    {
        "refund_no",
        "refund_id",
        "after_sale_no",
        "order_no",
        "amount",
        "order_refunded_amount",
        "order_paid_amount",
        "payment_status",
        "after_sale_status",
        "claim_status",
    }
)

#: ``order_created_payload``'s keys, per the contract module.
ORDER_PAYLOAD_KEYS = frozenset({"order_no", "payable_amount", "item_count"})

#: ``payment_settled_payload``'s keys, per the contract module.
PAYMENT_PAYLOAD_KEYS = frozenset({"payment_no", "order_no", "amount", "provider"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def events_of(shop: SeededShop, event_type: str, *, aggregate_id: int) -> list:
    """This merchant's rows of one ``event_type``, narrowed to one aggregate.

    Narrowed rather than counted because one fixture can legitimately produce several events
    of the same type (``paid_order`` settles one order and a test creates another) - a bare
    count would be asserting on the fixture rather than on the seam.
    """
    return [
        row
        for row in rows_for_merchant(merchant_id=shop.merchant_id, event_type=event_type)
        if row.aggregate_id == aggregate_id
    ]


def new_order(shop: SeededShop, *, lines=None) -> Order:
    """One committed order, on a marker that is fresh on every call.

    ``make_order`` derives both ``client_request_id`` and ``idempotency_key`` from its
    ``suffix``, so a constant suffix makes the second call an idempotent *replay* - it
    returns the first order and emits nothing. A per-call token is what makes "create an
    order" mean "create an order" every time.
    """
    return make_order(
        shop,
        lines=lines or [OrderLineInput(sku_id=shop.sku_ids[0], quantity=1)],
        suffix=f"seam-{uuid.uuid4().hex[:8]}",
    )


def provider_event_id(shop: SeededShop, label: str) -> str:
    """A provider event id that **carries the merchant marker**.

    Not cosmetic. ``payment_callbacks`` has no owner column, so ``purge_shop`` attributes the
    rows to sweep by the same rule the residue tool uses: a ``LIKE '%<marker>%'`` on
    ``provider_event_id``. An event id built from a bare random token therefore leaves the
    callback behind after teardown, and because the callback names an order that has been
    deleted it shows up as ``orphaned fixture callbacks (order gone)`` in
    ``scripts/residue.py`` - residue this suite created, which would poison the next
    measurement (measured: 26 rows after 26 runs of the payment seam test). Embedding the
    marker is the same thing the shared seed's own ``settle_order`` does with
    ``evt-<marker>-<suffix>``.

    ``test_provider_event_ids_carry_the_fixture_marker`` asserts the property, so a later edit
    to a label cannot silently reintroduce the leak.
    """
    return f"evt-{shop.marker}-{label}"[:128]


def payment_row(order_id: int) -> Payment:
    """The ``payments`` row for one order, read on a session this helper opens."""
    factory = get_session_factory()
    with factory() as session:
        row = session.execute(select(Payment).where(Payment.order_id == order_id)).scalars().one()
        session.expunge_all()
        return row


# ---------------------------------------------------------------------------
# 8. order.created - CreateOrderWorkflow step 9
# ---------------------------------------------------------------------------
def test_order_created_is_emitted_once_by_the_real_workflow(shop: SeededShop) -> None:
    """A successful create leaves **exactly one** ``order.created`` for that order.

    The payload keys are the contract (``{order_no, payable_amount, item_count}``) and the
    values are read back from the committed ``orders`` row - so an event announcing an amount
    the order does not hold fails here instead of looking plausible.

    **Mutation that must turn this red:** comment out the ``self._outbox.enqueue(...)`` call at
    step 9 (no row); pass ``aggregate_id=order.user_id``; or swap two payload arguments (the
    value assertions catch that last one, the key-set assertion cannot).
    """
    order = new_order(shop)

    events = events_of(shop, OutboxEventType.ORDER_CREATED.value, aggregate_id=int(order.id))
    assert len(events) == 1, "one order must queue exactly one order.created event"

    event = events[0]
    assert event.aggregate_type == OutboxAggregateType.ORDER.value
    assert event.merchant_id == shop.merchant_id
    # The emitter's own key, recorded verbatim - this is what an operator greps.
    assert event.idempotency_key.startswith(shop.marker)
    assert event.status == "PENDING"
    assert event.attempt_count == 0
    assert event.published_at is None

    payload = dict(event.payload)
    assert set(payload) == ORDER_PAYLOAD_KEYS
    assert payload["order_no"] == order.order_no
    assert payload["payable_amount"] == order.payable_amount
    assert payload["item_count"] == order.item_count
    assert assertion_that_no_secret_remains(payload) == []

    # The event describes a row that really is committed: read it on a fresh session.
    factory = get_session_factory()
    with factory() as session:
        stored = session.get(Order, order.id)
        assert stored is not None
        assert stored.order_no == payload["order_no"]


def test_a_replayed_order_create_emits_no_second_event(shop: SeededShop) -> None:
    """The same ``Idempotency-Key`` twice is still one order and one event.

    A replayed create returns the existing order **without reaching step 9** - the replay path
    returns in step 1 - so this is really an assertion about the seam's position: the block must
    stay below the replay return, where nothing has been created yet.

    **Mutation that must turn this red:** move the ``enqueue`` block into the replay path. A
    misplacement alone is *absorbed* by ``uq_event_type_aggregate`` (which is why the count is
    asserted rather than "no exception raised"); the mutation that exposes the duplication is
    moving the seam **and** dropping the unique index, and that pair was run.
    """
    token = uuid.uuid4().hex[:8]
    first = make_order(shop, suffix=f"replay-{token}")
    second = make_order(shop, suffix=f"replay-{token}")

    assert second.id == first.id, "the second call should have replayed the first order"

    events = events_of(shop, OutboxEventType.ORDER_CREATED.value, aggregate_id=int(first.id))
    assert len(events) == 1


def test_a_failed_create_emits_no_order_event(shop: SeededShop) -> None:
    """No order, no event - the negative control for the seam's position.

    ``test_writer.py`` proves this with the writer in isolation; this version proves it through
    the workflow's own rollback path. Both exist because they fail differently: a ``commit()``
    inside ``enqueue`` breaks both, while a seam moved *above* the INV-006 assertion (which runs
    after the stock reservation) breaks only this one.

    **Mutation that must turn this red:** any commit before the create fails, or moving the seam
    above step 7's assertion - either leaves an event with no order behind it.
    """
    from app.core.errors import InsufficientStockError
    from app.modules.order.service import OrderService

    token = uuid.uuid4().hex[:8]
    before = rows_for_merchant(
        merchant_id=shop.merchant_id, event_type=OutboxEventType.ORDER_CREATED.value
    )

    session = get_session_factory()()
    try:
        with pytest.raises(InsufficientStockError):
            # Far more units than the fixture's opening stock, so the reservation fails *after*
            # the idempotency claim, the address read and the pricing - i.e. deep inside the
            # transaction, with the order row already inserted.
            OrderService(session).create_order(
                principal=shop.consumer,
                items=[OrderLineInput(sku_id=shop.sku_ids[0], quantity=10_000)],
                address_id=shop.address_id,
                client_request_id=shop.client_request_id(f"seam-fail-{token}"),
                idempotency_key=shop.key(f"seam-fail-{token}"),
            )
    finally:
        session.close()

    after = rows_for_merchant(
        merchant_id=shop.merchant_id, event_type=OutboxEventType.ORDER_CREATED.value
    )
    assert [row.id for row in after] == [row.id for row in before]

    # The order really did not survive either - otherwise this test could pass on an unrelated
    # failure that happened before the seam was ever reached.
    factory = get_session_factory()
    with factory() as session:
        orders = (
            session.execute(select(Order.id).where(Order.merchant_id == shop.merchant_id))
            .scalars()
            .all()
        )
        assert orders == []


# ---------------------------------------------------------------------------
# 9. payment.settled - PaymentSuccessWorkflow step 10
# ---------------------------------------------------------------------------
def test_payment_settled_is_emitted_once_and_a_duplicate_delivery_adds_nothing(
    shop: SeededShop,
) -> None:
    """Exactly one ``payment.settled``, and **the same provider event delivered twice still
    leaves one**.

    The duplicate is a re-delivery of the *identical signed callback* - same ``event_id``, same
    bytes, same signature - because that is what a provider retry looks like. The second
    delivery must be answered as a duplicate and must not reach step 10 again.

    Two independent guards are under test and they are not interchangeable:

    * the **position** of the seam below the callback claim and the SUCCESS/PAID guards
      (``execution.duplicate is True`` is the evidence the second delivery never got past step 1);
    * the ``uq_event_type_aggregate`` backstop - which is why the assertion is a count of 1
      rather than "at most 1".

    **Mutation that must turn this red:** move the step-10 block above the ``SUCCESS``/``PAID``
    guards. On its own that is absorbed by ``enqueue``'s dedup, so the isolating mutation is
    performed as a pair with dropping ``uq_event_type_aggregate`` - and that pair was run.

    The negative control is inside the same test: the second delivery's ``applied`` is False, so
    a green run cannot mean "the duplicate was simply processed as a fresh settlement".
    """
    # A fresh order with **no settlement yet**: ``paid_order`` would already have settled this
    # one, and the first delivery below must be the one that applies the effect. (That is also
    # why the event id is ours: the seed's event id is already spent on the fixture's order.)
    order = new_order(shop)
    token = uuid.uuid4().hex[:8]
    event_id = provider_event_id(shop, f"seam-dup-{token}")

    # The first delivery is the seed's own settle helper (payment attempt + signed callback),
    # carrying **our** event id; the second re-sends that identical signed callback.
    first = settle_order(shop, order, suffix=f"seam-pay-{token}", event_id=event_id)
    assert first.applied, f"the first delivery should settle the payment: {first.status}"

    payment = payment_row(int(order.id))
    request = signed_success_callback(
        payment_no=payment.payment_no,
        order_no=order.order_no,
        amount=payment.amount,
        event_id=event_id,
        transaction_no=f"txn-seam-dup-{token}-b",
        provider=payment.channel,
    )
    session = get_session_factory()()
    try:
        duplicate = PaymentSuccessWorkflow(session).execute(request)
    finally:
        session.close()

    assert duplicate.duplicate is True, "a re-delivered event must be answered as a duplicate"
    assert duplicate.applied is False

    payment = payment_row(int(order.id))
    events = events_of(shop, OutboxEventType.PAYMENT_SETTLED.value, aggregate_id=int(payment.id))
    assert len(events) == 1, "a duplicate callback must not queue a second payment.settled"

    event = events[0]
    assert event.aggregate_type == OutboxAggregateType.PAYMENT.value
    assert event.merchant_id == shop.merchant_id
    # The emitter's key is the provider event, namespaced by provider - the thing an operator
    # searches for when a provider reports a delivery we cannot see.
    assert event.idempotency_key == f"{payment.channel}:{event_id}"

    payload = dict(event.payload)
    assert set(payload) == PAYMENT_PAYLOAD_KEYS
    assert payload["payment_no"] == payment.payment_no
    assert payload["order_no"] == order.order_no
    assert payload["amount"] == payment.amount
    assert payload["provider"] == payment.channel
    assert assertion_that_no_secret_remains(payload) == []

    # The payload describes what the database holds, read on a fresh session.
    refreshed = payment_row(int(order.id))
    assert refreshed.status == "SUCCESS"
    assert refreshed.amount == payload["amount"]
    assert refreshed.payment_no == payload["payment_no"]


def test_a_refused_callback_emits_no_payment_event(shop: SeededShop) -> None:
    """An amount-mismatched settlement is refused and commits nothing - so no event.

    The negative control for this seam: a refusal rolls back the whole transaction, and the
    outbox row is *in* that transaction. A seam above the amount guard (or an early commit)
    would leave the outbox announcing a settlement the payment row does not support - the
    "message about something that did not happen" failure the outbox exists to prevent.

    **Mutation that must turn this red:** commit inside ``enqueue`` (the event survives the
    refusal rollback) or move step 10 above step 3's amount check.
    """
    order = new_order(shop)
    before = rows_for_merchant(
        merchant_id=shop.merchant_id, event_type=OutboxEventType.PAYMENT_SETTLED.value
    )

    # ``amount`` is overridden by one minor unit, so the signature still verifies and the
    # *business* guard is what refuses it. A tampered body would be refused by the verifier and
    # would test the wrong layer.
    execution = settle_order(
        shop,
        order,
        suffix=f"mismatch-{uuid.uuid4().hex[:8]}",
        event_id=provider_event_id(shop, f"seam-mismatch-{uuid.uuid4().hex[:8]}"),
        amount=order.payable_amount + 1,
    )
    assert execution.applied is False
    assert execution.error_code == "PAYMENT_AMOUNT_MISMATCH", (
        f"expected the amount guard to refuse this delivery, got {execution.error_code!r}"
    )

    after = rows_for_merchant(
        merchant_id=shop.merchant_id, event_type=OutboxEventType.PAYMENT_SETTLED.value
    )
    assert after == before


# ---------------------------------------------------------------------------
# 10. refund.succeeded - RefundWorkflow step 7
# ---------------------------------------------------------------------------
def _approved_claim(seeded_shop: AfterSalesShop, *, item_indexes=(0,)):
    """File and approve a claim for the whole of the fixture's paid order."""
    factory = get_session_factory()
    with factory() as session:
        claim = seeded_shop.file_claim(
            session,
            amount=seeded_shop.paid_amount,
            item_indexes=item_indexes,
            quantities=tuple(3 for _ in item_indexes),
        )
        seeded_shop.approve(session, claim, amount=seeded_shop.paid_amount)
        return claim.after_sale_no


def test_refund_succeeded_carries_the_ten_contract_keys(seeded_shop: AfterSalesShop) -> None:
    """Exactly one ``refund.succeeded``, with exactly the ten keys the seam comment names.

    The key set is asserted with ``==`` against a literal frozenset: a *missing* key breaks a
    consumer, and an *extra* key is a payload that grew without anybody reviewing the contract.
    Both are failures here. The values are then checked against the committed rows (``refunds``
    / ``after_sales`` / ``orders``) rather than restated, so an event announcing the wrong
    amount fails instead of looking right.

    ``order_refunded_amount`` / ``order_paid_amount`` are the money axes **after** the refund -
    ``RefundWorkflow`` step 5's values - so this also catches a seam moved above
    ``_apply_money_axes`` (which would report ``0``).

    **Mutation that must turn this red:** comment out the step-7 ``enqueue``; drop one key from
    ``refund_succeeded_payload``; move the seam above the money-axis updates; pass ``refund_no``
    where ``refund_id`` belongs (the value assertions catch that, not the key set).
    """
    after_sale_no = _approved_claim(seeded_shop)
    factory = get_session_factory()

    with factory() as session:
        outcome = seeded_shop.refund(
            session,
            load_claim(session, after_sale_no=after_sale_no),
            amount=seeded_shop.paid_amount,
            suffix="seam-refund",
        )
        refund_id = int(outcome.refund.id)
        refund_no = outcome.refund.refund_no
        assert outcome.replayed is False

    events = events_of(
        seeded_shop.seeded, OutboxEventType.REFUND_SUCCEEDED.value, aggregate_id=refund_id
    )
    assert len(events) == 1, "one refund must queue exactly one refund.succeeded event"

    event = events[0]
    assert event.aggregate_type == OutboxAggregateType.REFUND.value
    assert event.merchant_id == seeded_shop.merchant_id
    # The refund's **own** idempotency key, as the seam comment requires - not a fresh uuid, so
    # a crash-and-retry is deduplicated the same way the refund row is.
    assert event.idempotency_key.startswith(seeded_shop.marker)
    assert "seam-refund" in event.idempotency_key

    payload = dict(event.payload)
    assert set(payload) == REFUND_PAYLOAD_KEYS, (
        "the refund payload's key set is a contract; "
        f"missing={sorted(REFUND_PAYLOAD_KEYS - set(payload))} "
        f"extra={sorted(set(payload) - REFUND_PAYLOAD_KEYS)}"
    )
    assert payload["refund_no"] == refund_no
    assert payload["refund_id"] == refund_id
    assert payload["after_sale_no"] == after_sale_no
    assert payload["order_no"] == seeded_shop.order_no
    assert payload["amount"] == seeded_shop.paid_amount
    # A full refund on both axes: refunded == paid, so both statuses are REFUNDED and the claim
    # is COMPLETED.
    assert payload["order_refunded_amount"] == seeded_shop.paid_amount
    assert payload["order_paid_amount"] == seeded_shop.paid_amount
    assert payload["payment_status"] == "REFUNDED"
    assert payload["after_sale_status"] == "REFUNDED"
    assert payload["claim_status"] == "COMPLETED"
    assert assertion_that_no_secret_remains(payload) == []

    # Every value in the payload is one the committed rows actually hold.
    with factory() as session:
        order = session.get(Order, seeded_shop.order_id)
        assert order is not None
        assert payload["order_refunded_amount"] == order.refunded_amount
        assert payload["order_paid_amount"] == order.paid_amount
        assert payload["payment_status"] == order.payment_status
        assert payload["after_sale_status"] == order.after_sale_status


def test_a_replayed_refund_emits_no_second_event(seeded_shop: AfterSalesShop) -> None:
    """The same ``Idempotency-Key`` replayed returns the existing refund - and one event.

    A retried ``POST /refunds`` is normal client behaviour, and the seam comment is explicit
    that a replay must not emit twice: the replay returns before step 7. This is the
    refund-direction twin of the payment duplicate test and is **not** redundant with it - the
    payment duplicate is answered by a different unique index in a different workflow, and the
    two could regress independently.

    **Mutation that must turn this red:** move the seam above the ``_existing_refund_for_key``
    early return, paired with dropping ``uq_event_type_aggregate`` (alone, ``enqueue`` absorbs
    the duplicate and the count stays 1). That pair was run.
    """
    after_sale_no = _approved_claim(seeded_shop)
    factory = get_session_factory()
    key = seeded_shop.key("seam-refund-replay")

    with factory() as session:
        first = seeded_shop.refund(
            session,
            load_claim(session, after_sale_no=after_sale_no),
            amount=seeded_shop.paid_amount,
            suffix="explicit-key",
            idempotency_key=key,
        )
        refund_id = int(first.refund.id)
        assert first.replayed is False

    with factory() as session:
        second = seeded_shop.refund(
            session,
            load_claim(session, after_sale_no=after_sale_no),
            amount=seeded_shop.paid_amount,
            suffix="explicit-key",
            idempotency_key=key,
        )
        assert second.replayed is True
        assert int(second.refund.id) == refund_id

    events = events_of(
        seeded_shop.seeded, OutboxEventType.REFUND_SUCCEEDED.value, aggregate_id=refund_id
    )
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 11. Every payload any seam produces is clean
# ---------------------------------------------------------------------------
def test_no_seam_payload_carries_a_sensitive_key(seeded_shop: AfterSalesShop) -> None:
    """All three events, produced by all three real workflows, pass the redaction assertion.

    One test rather than three because the property is about the **whole vocabulary**: a fourth
    event type added later without a payload review is caught only by an assertion that walks
    every row the seams produced. ``assertion_that_no_secret_remains`` returns the offending key
    *paths*, so a failure names the key rather than "a secret leaked".

    The rows are also required to be non-empty and to cover all three event types, because a
    sweep that passed over empty payloads (or over no rows at all) would report "clean" without
    having examined anything - the failure mode HANDOFF section 18.4 obligation 7 describes as a
    file that contributes zero tests.

    **Mutation that must turn this red:** add ``"token": <provider token>`` to any payload
    builder *and* bypass the writer's guard (a direct ``session.add(OutboxMessage(...))``),
    since a secret added to a builder is refused by ``enqueue`` at the workflow instead.
    """
    # -- produce the third event on top of the two `seeded_shop` already produced --
    after_sale_no = _approved_claim(seeded_shop)
    factory = get_session_factory()
    with factory() as session:
        seeded_shop.refund(
            session,
            load_claim(session, after_sale_no=after_sale_no),
            amount=seeded_shop.paid_amount,
            suffix="seam-sweep",
        )

    rows = rows_for_merchant(merchant_id=seeded_shop.merchant_id)
    assert rows, "the seams should have produced rows to sweep"
    seen = {row.event_type for row in rows}
    assert seen == {
        OutboxEventType.ORDER_CREATED.value,
        OutboxEventType.PAYMENT_SETTLED.value,
        OutboxEventType.REFUND_SUCCEEDED.value,
    }, f"all three event types should be represented, saw {sorted(seen)}"

    offenders: dict[int, list[str]] = {}
    for row in rows:
        payload = dict(row.payload)
        assert payload, f"{row.event_type} row {row.id} has an empty payload"
        leaks = assertion_that_no_secret_remains(payload)
        if leaks:
            offenders[int(row.id)] = leaks
    assert offenders == {}, f"sensitive key paths reached the outbox: {offenders}"

def test_provider_event_ids_carry_the_fixture_marker(shop: SeededShop) -> None:
    """The event-id shape the teardown relies on, asserted rather than remembered.

    ``purge_shop`` sweeps ``payment_callbacks`` by ``LIKE '%<marker>%'`` on
    ``provider_event_id`` (see :func:`provider_event_id`), so an event id without the marker
    is a row no cleanup can attribute - it survives teardown, names a deleted order, and is
    reported by ``scripts/residue.py`` as an orphaned fixture callback on every later run.

    **Mutation that must turn this red:** drop the marker from the id (return
    ``f"evt-{label}"``), which is exactly the leak that was measured and fixed.
    """
    event_id = provider_event_id(shop, "shape-check")
    assert shop.marker in event_id, "an unattributable event id cannot be swept by teardown"
    assert event_id.startswith("evt-"), "the residue tool only recognises marker-shaped ids"
    assert len(event_id) <= 128, "provider_event_id is VARCHAR(128)"
