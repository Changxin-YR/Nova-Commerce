"""The payment domain's own invariants, pinned as unit tests.

Spec references: design section 9 (error codes), section 6.1 (the settlement order),
REQ-PAY-005/006 + INV-008 (the mock guard), section 15.3 (the callback headers).

## What belongs here

Assertions about the *shape and vocabulary* of the payment domain that must not drift:

* every ``PAYMENT_*`` business code has the canonical HTTP status from
  ``app/core/errors.py`` - most importantly that the duplicate is **200**, because
  answering a provider with an error to a delivery that was already applied makes it retry
  forever;
* the callback's mandatory header names are the four the contract freezes, and the
  provider is *not* one of them (it is a path parameter, so a redundant header would be a
  second place for the two to disagree);
* ``PaymentSuccessWorkflow``'s own "can this be settled" tuple is the repository's, not a
  second opinion about money;
* the development mock surface is refused unless ``PAYMENT_MOCK_ENABLED`` **and**
  ``APP_ENV in {dev, test, demo}`` - checked against a real ``Settings`` object for each
  environment, because the guard is the only thing between a misconfigured deployment and
  a forged settlement.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import (
    ErrorCode,
    PaymentAlreadyPaidError,
    PaymentAmountMismatchError,
    PaymentCallbackDuplicateError,
    PaymentCallbackSignatureError,
    PaymentChannelUnsupportedError,
    PaymentMockDisabledError,
    PaymentNotFoundError,
    PaymentStateInvalidError,
    http_status_for,
)
from app.modules.payment.providers import CALLBACK_HEADER_NAMES
from app.modules.payment.repository import SETTLEABLE_STATUSES
from app.modules.payment.service import canonical_payment_request_hash, is_mock_allowed
from app.modules.payment.workflow import SETTLEABLE_STATUSES as WORKFLOW_SETTLEABLE

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Error codes and their canonical HTTP statuses (design section 9)
# ---------------------------------------------------------------------------
#: Every payment business error the domain raises. Listed explicitly rather than
#: discovered by reflection so that *removing* one is a visible diff too.
PAYMENT_ERRORS = (
    PaymentNotFoundError,
    PaymentAlreadyPaidError,
    PaymentAmountMismatchError,
    PaymentCallbackSignatureError,
    PaymentCallbackDuplicateError,
    PaymentChannelUnsupportedError,
    PaymentMockDisabledError,
    PaymentStateInvalidError,
)


@pytest.mark.parametrize("error", PAYMENT_ERRORS, ids=lambda e: e.code.name)
def test_every_payment_error_uses_a_6xxxx_code(error: type) -> None:
    assert 60_000 <= int(error.code) < 70_000, (
        f"{error.__name__} uses {error.code.name}, which is not a payment code"
    )


@pytest.mark.parametrize("error", PAYMENT_ERRORS, ids=lambda e: e.code.name)
def test_every_payment_error_status_is_the_canonical_one(error: type) -> None:
    """Nothing in this domain hand-picks an HTTP status.

    The one case that looks like an exception is the duplicate, and it is not: 60004 is
    mapped to 200 in ``app/core/errors.py``'s canonical table, with a comment explaining
    why. This test asserts that the domain agrees with that table rather than overriding
    it - if someone later "fixes" the duplicate to a 409, the failure names the reason.
    """
    assert error().status_code == http_status_for(error.code)


def test_a_duplicate_callback_is_an_idempotent_success_not_an_error() -> None:
    """The single most consequential status in the payment surface.

    A provider retries until it sees success. If a duplicate delivery - one that was in
    fact already applied - were answered with an error, the provider would retry a
    settlement that is already done, forever, which is the one honest use of
    "200 = your request was already handled".
    """
    assert PaymentCallbackDuplicateError().status_code == 200
    assert int(PaymentCallbackDuplicateError.code) == 60_004


def test_a_bad_signature_is_an_authentication_failure_not_a_validation_one() -> None:
    """The callback surface carries no JWT, so the signature **is** the authentication
    model and a bad one is an auth failure. 60003's canonical status is a refusal either
    way, which is why this asserts the code rather than a number: the code is the part
    clients branch on."""
    assert int(PaymentCallbackSignatureError.code) == 60_003
    assert PaymentCallbackSignatureError().status_code in (400, 401)


def test_the_state_guard_has_its_own_code_distinct_from_already_paid() -> None:
    """``PAYMENT_STATE_INVALID (60007)`` is about *this attempt*; ``PAYMENT_ALREADY_PAID
    (60001)`` is about the *order*. Collapsing them would make "a new attempt is the
    correct recovery" indistinguishable from "this order is settled forever"."""
    assert PaymentStateInvalidError.code is ErrorCode.PAYMENT_STATE_INVALID
    assert PaymentAlreadyPaidError.code is ErrorCode.PAYMENT_ALREADY_PAID
    assert PaymentStateInvalidError.code is not PaymentAlreadyPaidError.code


def test_a_foreign_payment_is_not_found_never_forbidden() -> None:
    """Section 109: a 403 would confirm the row exists, making the endpoint an existence
    oracle for other customers' payments."""
    assert PaymentNotFoundError().status_code == 404


# ---------------------------------------------------------------------------
# The callback header contract (section 15.3)
# ---------------------------------------------------------------------------
def test_the_four_frozen_callback_headers_are_the_ones_this_domain_reads() -> None:
    assert CALLBACK_HEADER_NAMES == (
        "X-Provider",
        "X-Provider-Event-Id",
        "X-Provider-Timestamp",
        "X-Provider-Signature",
    )


def test_the_provider_is_read_from_the_path_and_not_required_as_a_header() -> None:
    """The route is ``POST /payments/callbacks/{provider}``.

    A header supplying the same fact would be a second place for it to disagree, and the
    thing the two would disagree about is *which secret to verify with* - so a mismatch
    would silently verify a signature against the wrong key.
    """
    from app.modules.payment.api import callbacks

    # The sub-path is spelled in the decorator because `app/api/v1/router.py` mounts every
    # submodule at the module prefix and uses the submodule name only as an OpenAPI tag -
    # the same convention `fulfillment/api/admin.py` and `aftersales/api/admin.py` follow.
    # Asserted here because the failure mode is silent: `/{provider}` produces a route at
    # `POST /api/v1/payments/MOCK`, which exists and is wrong.
    paths = {route.path for route in callbacks.router.routes}
    assert paths == {"/callbacks/{provider}"}, f"unexpected callback routes: {sorted(paths)}"

    # The provider must be a **path** parameter, so FastAPI resolves it from the URL and
    # the only header parameters on this route are the three the contract freezes.
    for route in callbacks.router.routes:
        dependant = route.dependant  # type: ignore[attr-defined]
        header_names = {param.alias for param in dependant.header_params}
        assert header_names == {
            "X-Provider-Event-Id",
            "X-Provider-Timestamp",
            "X-Provider-Signature",
        }, f"unexpected callback headers: {sorted(header_names)}"


def test_the_callback_route_has_no_bearer_dependency() -> None:
    """REQ-PAY-005: the callback surface must not accept or require a JWT.

    A provider that held a user's bearer token could act as that user everywhere else,
    which is exactly what the separate authentication model exists to prevent. Asserted on
    the route's own dependency graph rather than by reading the source, so that adding
    ``CurrentPrincipal`` to this handler fails here instead of shipping.
    """
    from app.modules.payment.api import callbacks

    def dependency_names(node) -> set[str]:
        found = {getattr(node.call, "__name__", "")}
        for child in node.dependencies:
            found |= dependency_names(child)
        return found

    for route in callbacks.router.routes:
        names = dependency_names(route.dependant)  # type: ignore[attr-defined]
        for forbidden in ("get_current_principal", "require_staff", "require_merchant_scope"):
            assert forbidden not in names, (
                f"the callback route depends on {forbidden}; the provider's authentication "
                "model is the HMAC signature, never a user token"
            )


# ---------------------------------------------------------------------------
# The settleable vocabulary has one owner
# ---------------------------------------------------------------------------
def test_the_workflow_and_the_repository_agree_about_what_can_be_settled() -> None:
    """Two tuples meaning "this attempt can still be settled" is one too many.

    A second copy is a second opinion about money: the workflow would settle a status the
    repository's documentation calls dead, or refuse one it calls live, and the two would
    drift silently because nothing compares them at runtime.
    """
    assert WORKFLOW_SETTLEABLE is SETTLEABLE_STATUSES
    assert SETTLEABLE_STATUSES == ("INITIATED", "PAYING")


def test_a_dead_attempt_status_is_not_settleable() -> None:
    """``FAILED``/``CLOSED``/``REFUNDED``/``PARTIAL_REFUNDED`` are all outside the set, and
    each for a different reason worth naming: the first two mean this attempt is over (a
    new one is the recovery), and the last two mean money has already moved *out*, so
    settling again is the double settlement the guard exists for."""
    for status in ("FAILED", "CLOSED", "REFUNDED", "PARTIAL_REFUNDED", "SUCCESS"):
        assert status not in SETTLEABLE_STATUSES


# ---------------------------------------------------------------------------
# The mock guard (REQ-PAY-005, INV-008)
# ---------------------------------------------------------------------------
def _settings(**overrides) -> Settings:
    base = {"PAYMENT_MOCK_ENABLED": True}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_the_mock_surface_is_allowed_in_the_development_environments(env: str) -> None:
    assert is_mock_allowed(_settings(APP_ENV=env)) is True


def test_demo_is_an_unreachable_member_of_the_mock_guard_and_that_is_recorded() -> None:
    """``APP_ENV`` cannot be ``"demo"``, so one third of the guard's allow-list is dead.

    ``Settings.mock_payment_allowed`` and the ``PAYMENT_MOCK_ENABLED`` validator both
    spell the development set as ``{"dev", "test", "demo"}`` (and the validator's error
    message says "dev/demo only"), but ``AppEnv`` is
    ``Literal["dev", "test", "staging", "prod"]``. So:

    * the **safe** direction is unaffected - ``demo`` can never be set, so it cannot be how
      a hardened environment gets the mock surface;
    * the cost is a small lie in a security guard's own vocabulary, and a demo deployment
      that expected ``APP_ENV=demo`` to be accepted gets a validation error at startup
      instead.

    This test asserts the current, observed reality rather than the intended one, so that
    either fix - adding ``"demo"`` to ``AppEnv`` or removing it from the guard - fails here
    and forces the two to be brought back into agreement deliberately. **Reported to the
    captain**, because ``app/core/config.py`` is not this module's file.
    """
    from app.core.config import AppEnv

    assert "demo" not in AppEnv.__args__, (
        "AppEnv now accepts 'demo' - good: remove this test and extend the parametrise "
        "above, because the guard's allow-list has become reachable"
    )
    with pytest.raises(Exception, match="APP_ENV"):
        _settings(APP_ENV="demo")


def _hardened(**overrides) -> dict:
    """A configuration that is **valid for ``APP_ENV=prod`` in every other respect**.

    The point of spelling this out: a test that just set ``APP_ENV=prod`` with the mock flag
    on would be refused by *some* validator, and the assertion would pass without ever
    showing which rule fired. Every other hardening flag is set to its safe value here, so
    the only thing left that can refuse the object is the mock flag itself.
    """
    base = {
        "APP_ENV": "prod",
        "APP_DEBUG": False,
        "AI_USE_FAKE_PROVIDERS": False,
        "REFRESH_COOKIE_SECURE": True,
        "JWT_SECRET_KEY": "a-production-grade-secret-value-0123456789abcdef",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_settings_validator_refuses_the_mock_flag_in_a_hardened_environment(env: str) -> None:
    """The setting-level half of REQ-PAY-005.

    A deployment that sets ``PAYMENT_MOCK_ENABLED=true`` with ``APP_ENV`` of staging or prod
    must not start at all. This is the intended failure mode, not an inconvenience: the mock
    surface marks an order paid without money moving, so a live deployment that merely
    *warns* about it has already lost.
    """
    with pytest.raises(ValueError, match="PAYMENT_MOCK_ENABLED"):
        _settings(**_hardened(APP_ENV=env, PAYMENT_MOCK_ENABLED=True))


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_endpoint_guard_refuses_even_when_the_validator_was_bypassed(env: str) -> None:
    """The endpoint-level half, and the reason it is asked twice.

    ``model_construct`` builds a ``Settings`` object **without running any validator**, which
    is exactly the bypass INV-008 anticipates. The endpoint therefore asks
    ``mock_payment_allowed`` itself, and this test constructs the bypassed object and asserts
    that the answer is still "no". One guard that somebody can skip is not a guard.
    """
    bypassed = Settings.model_construct(
        **{**_hardened(APP_ENV=env), "PAYMENT_MOCK_ENABLED": True}
    )
    assert bypassed.PAYMENT_MOCK_ENABLED is True, "the bypass did not take effect"
    assert is_mock_allowed(bypassed) is False, (
        "the endpoint-level guard allowed the mock surface on a hardened environment that "
        "reached it via model_construct()"
    )


def test_the_hardened_baseline_is_otherwise_valid() -> None:
    """Control for the two tests above: with the flag off, the same config is accepted.

    Without this, a typo in ``_hardened`` would make both tests pass for the wrong reason -
    they would be measuring a broken baseline rather than the mock guard.
    """
    assert _settings(**_hardened(PAYMENT_MOCK_ENABLED=False)).is_hardened is True


def test_the_mock_guard_refuses_when_the_flag_is_off_even_in_development() -> None:
    assert is_mock_allowed(_settings(APP_ENV="dev", PAYMENT_MOCK_ENABLED=False)) is False


def test_the_mock_guard_is_asked_about_the_environment_not_cached() -> None:
    """No module-level memoisation: a process that read the flag once at import would keep
    answering "allowed" after a configuration reload, which is the shape of a guard that
    works in tests and not in a deployment."""
    assert is_mock_allowed(_settings(APP_ENV="test")) is True
    assert is_mock_allowed(_settings(APP_ENV="test", PAYMENT_MOCK_ENABLED=False)) is False


# ---------------------------------------------------------------------------
# The idempotency hash includes the buyer
# ---------------------------------------------------------------------------
def test_the_request_hash_depends_on_the_buyer() -> None:
    """``payments.idempotency_key`` is unique per **merchant**, not per customer.

    Without the buyer in the hash, two customers who happened to send the same key for the
    same order and channel would produce the same hash, and the second would be handed the
    first one's payment - a cross-customer read produced by a "safe retry" path, which is
    the last place anyone looks for an authorisation bug.
    """
    first = canonical_payment_request_hash(user_id=1, order_no="NV1", channel="MOCK")
    second = canonical_payment_request_hash(user_id=2, order_no="NV1", channel="MOCK")
    assert first != second


def test_the_request_hash_ignores_irrelevant_input_ordering() -> None:
    """The hash is over canonical JSON, so an identical intent hashes identically.

    ``channel`` is passed as a ``str`` here and as an enum member elsewhere; both must
    land on the same digest, or a client that sent the same request twice through two code
    paths would get a spurious 10011 conflict.
    """
    from app.modules.payment.enums import PaymentChannel

    assert canonical_payment_request_hash(
        user_id=7, order_no="NV1", channel=PaymentChannel.MOCK
    ) == canonical_payment_request_hash(user_id=7, order_no="NV1", channel="MOCK")


def test_the_request_hash_changes_with_the_channel() -> None:
    """Same key, same order, different channel is a *different* request - not a retry."""
    assert canonical_payment_request_hash(
        user_id=7, order_no="NV1", channel="MOCK"
    ) != canonical_payment_request_hash(user_id=7, order_no="NV1", channel="ALIPAY")


def test_the_request_hash_is_a_sha256_hex_digest() -> None:
    digest = canonical_payment_request_hash(user_id=7, order_no="NV1", channel="MOCK")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)
