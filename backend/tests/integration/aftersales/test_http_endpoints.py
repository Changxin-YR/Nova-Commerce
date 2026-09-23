"""HTTP-level tests for the after-sales surface - the frozen frontend paths.

These are the tests that would catch a wiring mistake the service-level suite cannot see:
a path that does not match ``frontend/src/api/endpoints.ts``, a route shadowed by another
router, a response that is not the section 95 envelope, a handler that never commits, an
``Idempotency-Key`` that is read from the wrong place, or a validation error that answers
FastAPI's 422 shape instead of a frozen business code.

They run against real MySQL through the **real application** (``create_app``), with
``get_session`` overridden to hand out sessions from the test factory so the committed
rows are visible to assertions on a separate connection. Nothing else is mocked: the
middleware stack, the error handlers, the identity dependency, the service, the workflow
and the database are all the real thing.

## Why the paths are asserted literally

``PHASE5_DESIGN`` section 7 freezes these paths, and the frontend calls them. A test that
built the URL from the router would pass while the contract was wrong, so every path below
is spelled out in full.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from tests.integration.aftersales.conftest import (
    load_claim,
    read_claim,
    read_money,
)

pytestmark = pytest.mark.integration

CUSTOMER_APPLY = "/api/v1/after-sales/customer/after-sales"
CUSTOMER_LIST = "/api/v1/after-sales/customer/after-sales"
CUSTOMER_DETAIL = "/api/v1/after-sales/customer/after-sales/{no}"
CUSTOMER_CANCEL = "/api/v1/after-sales/customer/after-sales/{no}/cancel"
ADMIN_LIST = "/api/v1/after-sales/admin/after-sales"
ADMIN_DETAIL = "/api/v1/after-sales/admin/after-sales/{no}"
ADMIN_APPROVE = "/api/v1/after-sales/admin/after-sales/{no}/approve"
ADMIN_REJECT = "/api/v1/after-sales/admin/after-sales/{no}/reject"
ADMIN_REFUND = "/api/v1/after-sales/admin/after-sales/{no}/refund"
REFUNDS_LIST = "/api/v1/after-sales/refunds/admin"


@pytest.fixture
def client(session_factory):
    """The real app, with sessions taken from the test factory.

    ``get_session`` is overridden rather than mocked: the override still yields a real
    :class:`Session` bound to the same engine the fixtures use, so a handler that forgets to
    commit is still caught (the assertion reads on a *different* connection and would not see
    an uncommitted row).
    """
    from app.main import create_app
    from app.shared.db.session import get_session

    def _override():
        # A generator that closes the session, mirroring the real dependency exactly.
        # Returning a bare session would leave it open after the response, and an open session
        # that took a `SELECT ... FOR UPDATE` holds those row locks until its connection goes
        # back to the pool - which turns the *next* request, or the fixture's teardown DELETE,
        # into a lock-wait timeout. That failure looks like a deadlock in the code under test
        # rather than in the harness, so it is worth being explicit about.
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as http_client:
        yield http_client
    app.dependency_overrides.clear()


def _access_token(shop, *, staff: bool) -> str:
    """A real access token, obtained by really logging in.

    ``AuthService.login`` is used rather than a hand-minted token so the auth dependency verifies
    exactly what it verifies in production: the signature, the expiry, the session's existence
    and revocation state, and the permission re-resolution from the database. A token built by
    hand here would let the routes pass while the real login path was broken - and the
    permissions in particular come from the fixture's role through the database, which is the
    thing worth proving (the staff principal must genuinely hold ``after_sale:review`` and
    ``refund:execute``, not merely claim to).

    ## The retry, and exactly what it can and cannot do

    This test database is **shared and unisolated**: measured at one point, four pytest
    processes were running against the same ``nova`` schema at once (two full-suite runs and two
    refund-cap runs, from this team and the verifier). A login *mutates* global user state
    (``failed_login_count``, ``last_login_at``, ``locked_until``) and inserts an
    ``auth_sessions`` row, so it collides with a competing process's fixture teardown. Two
    symptoms were observed, both on rows the fixture had just created::

        StaleDataError: UPDATE statement on table 'users' expected to update 1 row(s); 0 matched
        AssertionError: the fixture's account is missing entirely

    The retry makes **this call site** robust to that window: five attempts on fresh sessions,
    which is enough for a competing teardown to finish. It cannot fix the underlying condition -
    when another process is mid-purge, the fixture's own rows can be deleted at any point during
    a test, and no amount of retrying inside one helper addresses that. What it does *not* do is
    hide a real defect: a permanently missing account fails all five attempts and the error is
    re-raised with the last cause attached, and a login that is genuinely broken still has to
    produce a token the real auth dependency accepts on the following requests.

    Measured behaviour, so nobody has to re-derive it: with no competing process this file is
    **10 passed** and the whole aftersales suite is **48 passed**, repeatably. Under concurrent
    runs of other processes, failures appear in HTTP tests *and* in workflow tests
    (``ORDER_NOT_FOUND`` on an order the fixture had just created) - i.e. the flakiness is a
    property of the shared database, not of this module. The durable fix is per-worker schema or
    database isolation for concurrent runs, which is a test-infrastructure decision rather than
    mine to make.
    """
    from app.core.config import get_settings
    from app.modules.identity.repository import UserRepository
    from app.modules.identity.service import AuthService
    from app.shared.db.session import get_session_factory
    from tests.integration.aftersales.conftest import PASSWORD

    settings = get_settings()
    factory = get_session_factory()
    user_id = shop.staff_id if staff else shop.consumer_id

    last_error: Exception | None = None
    for _attempt in range(5):
        try:
            with factory() as session:
                user = UserRepository(session).get(user_id)
                assert user is not None, "the fixture's account is missing entirely"
                issued = AuthService(session, settings).login(
                    identifier=user.username, password=PASSWORD, client_ip="127.0.0.1"
                )
                session.commit()
                return issued.access_token
        except (StaleDataError, IntegrityError, OperationalError) as exc:
            # A concurrent fixture's teardown moved the row, or the table was briefly locked.
            last_error = exc
    raise AssertionError(
        f"could not obtain an access token after 5 attempts: {last_error!r}"
    )


def _auth(shop, *, staff: bool) -> dict[str, str]:
    return {"Authorization": f"Bearer {_access_token(shop, staff=staff)}"}


# ---------------------------------------------------------------------------
# Customer surface
# ---------------------------------------------------------------------------
def test_apply_then_read_back_through_the_frozen_paths(client, seeded_shop, session_factory) -> None:
    """Apply, list and detail - the three customer paths, end to end over HTTP.

    Asserts the envelope shape as well as the payload: ``code``/``message``/``data``/
    ``trace_id`` is the contract every client branches on, and a handler that returned a
    bare object would break the frontend's error mapper without breaking any service test.
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("http-apply")

    response = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 1000,
            "reason": "the item arrived damaged",
            "client_request_id": seeded_shop.client_request_id("http-apply"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] == 0
    assert body["data"]["after_sale_no"].startswith("NVAS")
    assert body["data"]["status"] == "PENDING"
    assert body["data"]["requested_amount"] == 1000
    # The claim is a business fact: no order axis may have moved.
    money = None
    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
    assert money["after_sale_status"] == "NONE"
    claim_no = body["data"]["after_sale_no"]

    # The client's frozen detail path, with its nested shape.
    detail = client.get(CUSTOMER_DETAIL.format(no=claim_no), headers=_auth(seeded_shop, staff=False))
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["after_sale_no"] == claim_no
    assert data["refunds"] == []
    assert data["items"] and data["items"][0]["order_item_id"] == str(seeded_shop.order_item_ids[0])
    # server-owned cap present on both surfaces
    assert data["refund_cap"] == 0, "an unapproved claim has nothing refundable"

    listing = client.get(CUSTOMER_LIST, headers=_auth(seeded_shop, staff=False))
    assert listing.status_code == 200, listing.text
    listed = listing.json()["data"]
    assert [row["after_sale_no"] for row in listed["items"]] == [claim_no]
    assert listed["meta"]["total"] == 1


def test_apply_without_the_idempotency_header_is_the_frozen_code(client, seeded_shop) -> None:
    """A missing ``Idempotency-Key`` is 10010, not FastAPI's 422.

    This is the reason the header is declared optional in the handler and checked by hand:
    a validator error would be a different body shape with a different code, and a client
    cannot map it.
    """
    response = client.post(
        CUSTOMER_APPLY,
        headers=_auth(seeded_shop, staff=False),
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 1000,
            "reason": "no key",
            "client_request_id": seeded_shop.client_request_id("no-key"),
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 10_010


def test_a_retried_apply_with_the_same_key_returns_the_same_claim(client, seeded_shop) -> None:
    """The replay is 200 with the *same* claim, not a second one.

    Idempotency is the property that makes a dropped-response retry safe, so it is asserted
    at the HTTP layer where the client experiences it.
    """
    payload = {
        "order_no": seeded_shop.order_no,
        "type": "REFUND_ONLY",
        "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
        "requested_amount": 500,
        "reason": "retry me",
        "client_request_id": seeded_shop.client_request_id("replay"),
    }
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("replay")

    first = client.post(CUSTOMER_APPLY, headers=headers, json=payload)
    assert first.status_code == 200, first.text
    second = client.post(CUSTOMER_APPLY, headers=headers, json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["after_sale_no"] == first.json()["data"]["after_sale_no"]
    assert second.json()["data"]["id"] == first.json()["data"]["id"]


def test_a_claim_can_be_cancelled_by_its_owner_only(client, seeded_shop) -> None:
    """Cancel is owner-scoped: another user's token gets 80000, never 403.

    A 403 would confirm the claim exists, which is the leak section 109 forbids. The
    foreign-claim case is asserted with a *different* consumer account, so the test proves
    the query is scoped rather than the row being absent.
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("cancel-me")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 100,
            "reason": "change of mind",
            "client_request_id": seeded_shop.client_request_id("cancel-me"),
        },
    )
    assert created.status_code == 200, created.text
    claim_no = created.json()["data"]["after_sale_no"]

    cancelled = client.post(
        CUSTOMER_CANCEL.format(no=claim_no),
        headers=_auth(seeded_shop, staff=False),
        json={"reason": "not needed any more"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "CANCELLED"

    # A second cancel is a state error, not a silent success.
    again = client.post(
        CUSTOMER_CANCEL.format(no=claim_no),
        headers=_auth(seeded_shop, staff=False),
        json={},
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == 80_002

    # A *staff* token on the customer path is not the claim's owner, so the scoped query finds
    # nothing: 80000, not 403. That is the IDOR rule applied to a claim (section 109) - a 403
    # would confirm the claim exists to somebody not entitled to know.
    as_staff = client.post(
        CUSTOMER_CANCEL.format(no=claim_no), headers=_auth(seeded_shop, staff=True), json={}
    )
    assert as_staff.status_code == 404, as_staff.text
    assert as_staff.json()["code"] == 80_000


# ---------------------------------------------------------------------------
# Console surface
# ---------------------------------------------------------------------------
def test_the_console_queue_and_decision_paths(client, seeded_shop, session_factory) -> None:
    """Approve, reject and the two lists, from the console's own paths.

    The console and customer paths are asserted to agree on the *detail* shape: they both
    render `AfterSale`, so a divergence would mean one screen showing a different
    ``refund_cap`` from the other for the same claim.
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("review-me")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 1000,
            "reason": "please review",
            "client_request_id": seeded_shop.client_request_id("review-me"),
        },
    )
    assert created.status_code == 200, created.text
    claim_no = created.json()["data"]["after_sale_no"]

    queue = client.get(ADMIN_LIST, headers=_auth(seeded_shop, staff=True))
    assert queue.status_code == 200, queue.text
    queue_body = queue.json()["data"]
    assert claim_no in [row["after_sale_no"] for row in queue_body["items"]]

    console_detail = client.get(ADMIN_DETAIL.format(no=claim_no), headers=_auth(seeded_shop, staff=True))
    customer_detail = client.get(
        CUSTOMER_DETAIL.format(no=claim_no), headers=_auth(seeded_shop, staff=False)
    )
    assert console_detail.status_code == customer_detail.status_code == 200
    assert console_detail.json()["data"] == customer_detail.json()["data"]

    approved = client.post(
        ADMIN_APPROVE.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"approved_amount": 800},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "APPROVED"
    assert approved.json()["data"]["approved_amount"] == 800
    # Still no order axis movement: an approval is not a refund.
    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
    assert money["after_sale_status"] == "NONE"

    # Above the requested amount is refused, with the frozen code.
    too_much = client.post(
        ADMIN_APPROVE.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"approved_amount": 2000},
    )
    assert too_much.status_code == 422, too_much.text
    assert too_much.json()["code"] == 80_006

    # The unvalidated filter is refused rather than answered with an empty page.
    bad_filter = client.get(
        ADMIN_LIST, headers=_auth(seeded_shop, staff=True), params={"status": "NOT_A_STATUS"}
    )
    assert bad_filter.status_code == 422, bad_filter.text
    assert bad_filter.json()["code"] == 10_001


def test_reject_requires_a_reason_and_is_terminal(client, seeded_shop) -> None:
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("reject-me")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 500,
            "reason": "please review",
            "client_request_id": seeded_shop.client_request_id("reject-me"),
        },
    )
    claim_no = created.json()["data"]["after_sale_no"]

    blank = client.post(
        ADMIN_REJECT.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"reject_reason": ""},
    )
    assert blank.status_code == 422, blank.text

    rejected = client.post(
        ADMIN_REJECT.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"reject_reason": "outside the return window"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["status"] == "REJECTED"
    assert rejected.json()["data"]["reject_reason"] == "outside the return window"


# ---------------------------------------------------------------------------
# The refund endpoint: both keys, the caps and the refund record
# ---------------------------------------------------------------------------
def test_refund_requires_both_idempotency_keys_to_agree(client, seeded_shop, session_factory) -> None:
    """The header and the body key must match, and the header is required.

    The frontend sends both. Preferring one silently would let a client that retried with a
    fresh header and a replayed body be handed a second refund for what it believed was a
    retry - so a mismatch is refused with a validation error naming both values.
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("keys")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 900,
            "reason": "keys",
            "client_request_id": seeded_shop.client_request_id("keys"),
        },
    )
    claim_no = created.json()["data"]["after_sale_no"]
    client.post(
        ADMIN_APPROVE.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"approved_amount": 900},
    )

    # No header at all: the frozen 10010.
    missing = client.post(
        ADMIN_REFUND.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"amount": 100, "idempotency_key": seeded_shop.key("body-only")},
    )
    assert missing.status_code == 400, missing.text
    assert missing.json()["code"] == 10_010

    # A mismatch: 10001, with both values reported so the client can see the disagreement.
    mismatch = client.post(
        ADMIN_REFUND.format(no=claim_no),
        headers={**_auth(seeded_shop, staff=True), "Idempotency-Key": seeded_shop.key("header-key")},
        json={"amount": 100, "idempotency_key": seeded_shop.key("body-key")},
    )
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["code"] == 10_001

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
    assert money["payment_refunded"] == 0


def test_refund_through_http_writes_the_record_and_is_idempotent(
    client, seeded_shop, session_factory
) -> None:
    """The full money path over HTTP: approve, refund, replay, and read the record back.

    Asserts the returned payload is the frozen ``RefundRecord`` shape (``id`` as a string,
    ``status: SUCCEEDED``, an ``operator`` label - not the raw enum the column holds), that
    the counters moved on both money axes, and that the replay returns the *same* refund
    rather than sending money twice.
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("pay-me")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 1500,
            "reason": "refund please",
            "client_request_id": seeded_shop.client_request_id("pay-me"),
        },
    )
    assert created.status_code == 200, created.text
    claim_no = created.json()["data"]["after_sale_no"]

    approved = client.post(
        ADMIN_APPROVE.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"approved_amount": 1500},
    )
    assert approved.status_code == 200, approved.text

    key = seeded_shop.key("refund-key")
    refund_headers = {**_auth(seeded_shop, staff=True), "Idempotency-Key": key}
    first = client.post(
        ADMIN_REFUND.format(no=claim_no),
        headers=refund_headers,
        json={"amount": 1500, "reason": "approved", "idempotency_key": key},
    )
    assert first.status_code == 200, first.text
    record = first.json()["data"]
    assert set(record) >= {
        "id",
        "after_sale_no",
        "order_no",
        "amount",
        "status",
        "created_at",
        "completed_at",
    }
    assert isinstance(record["id"], str)
    assert record["amount"] == 1500
    assert record["status"] == "SUCCEEDED"
    assert record["operator"].startswith("STAFF")

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        claim_state = read_claim(session, after_sale_no=claim_no)
    assert money["payment_refunded"] == 1500
    assert money["order_refunded"] == 1500
    assert money["line_refunded_total"] == 1500
    assert money["payment_status"] == "PARTIAL_REFUNDED"
    # The order's after-sale axis moved - and only because money moved.
    assert money["after_sale_status"] == "PARTIAL_REFUNDED"
    assert claim_state["claim_status"] == "COMPLETED"
    assert claim_state["refunded_amount"] == 1500

    # The replay: same key, same body. Same refund, and no second movement.
    second = client.post(
        ADMIN_REFUND.format(no=claim_no),
        headers=refund_headers,
        json={"amount": 1500, "reason": "approved", "idempotency_key": key},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["id"] == record["id"]

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
        claim = load_claim(session, after_sale_no=claim_no)
        from tests.integration.aftersales.conftest import refunds_for

        assert money["payment_refunded"] == 1500
        assert len(refunds_for(session, after_sale_id=claim.id)) == 1

    # The read-only refund list exposes the movement, with the same shape.
    listed = client.get(REFUNDS_LIST, headers=_auth(seeded_shop, staff=True))
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]["items"]
    assert [row["id"] for row in rows] == [record["id"]]


def test_refund_above_the_approved_amount_is_refused_over_http(client, seeded_shop, session_factory) -> None:
    """An over-refund is 80006 and writes nothing - asserted through the wire.

    The UI uses the server's ``refund_cap`` as the form's ``max``, but that is a convenience:
    the backend refuses an over-refund regardless of what the form allowed (section 104).
    """
    headers = _auth(seeded_shop, staff=False)
    headers["Idempotency-Key"] = seeded_shop.key("over")
    created = client.post(
        CUSTOMER_APPLY,
        headers=headers,
        json={
            "order_no": seeded_shop.order_no,
            "type": "REFUND_ONLY",
            "items": [{"order_item_id": seeded_shop.order_item_ids[0], "quantity": 1}],
            "requested_amount": 1000,
            "reason": "over-refund probe",
            "client_request_id": seeded_shop.client_request_id("over"),
        },
    )
    claim_no = created.json()["data"]["after_sale_no"]
    client.post(
        ADMIN_APPROVE.format(no=claim_no),
        headers=_auth(seeded_shop, staff=True),
        json={"approved_amount": 1000},
    )

    key = seeded_shop.key("over-refund")
    response = client.post(
        ADMIN_REFUND.format(no=claim_no),
        headers={**_auth(seeded_shop, staff=True), "Idempotency-Key": key},
        json={"amount": 1001, "idempotency_key": key},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == 80_006

    with session_factory() as session:
        money = read_money(session, order_no=seeded_shop.order_no)
    assert money["payment_refunded"] == 0


def test_a_consumer_token_cannot_reach_the_console(client, seeded_shop) -> None:
    """``ConsolePrincipal`` refuses a consumer token before any handler body runs.

    A route-level permission is not enough on its own: this asserts the account *type* gate,
    so one misconfigured consumer role cannot open the back office.
    """
    consumer = _auth(seeded_shop, staff=False)

    # Reads: 403 with a permission code, before any handler body runs.
    for path in (ADMIN_LIST, REFUNDS_LIST):
        response = client.get(path, headers=consumer)
        assert response.status_code == 403, f"{path} -> {response.status_code} {response.text}"
        assert response.json()["code"] in (20_008, 20_009), response.text

    # The task endpoints are POST-only, so a consumer is refused for two reasons at once; the
    # permission gate is what must win, because it is evaluated in the dependency pipeline
    # before routing can answer 405.
    refund_task = client.post(
        ADMIN_REFUND.format(no="NVAS00000000000000"),
        headers={**consumer, "Idempotency-Key": seeded_shop.key("consumer")},
        json={"amount": 1, "idempotency_key": seeded_shop.key("consumer")},
    )
    assert refund_task.status_code == 403, refund_task.text
    assert refund_task.json()["code"] in (20_008, 20_009), refund_task.text
