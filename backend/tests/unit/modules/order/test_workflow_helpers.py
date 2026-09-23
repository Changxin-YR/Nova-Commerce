"""Workflow helpers - the deterministic pieces of the create transaction.

PHASE4_DESIGN §7, spec §38/§41/§48, INV-003/INV-015.

These are pure functions, so they are tested without a database. Two of them carry
properties that are easy to get wrong in a way no integration test would notice:

* :func:`merge_order_lines` - merging **before** pricing is a correctness step, not
  housekeeping: a per-unit promotion applied to two separate lines of the same SKU is
  not guaranteed to equal the same promotion applied to the summed quantity, and
  ``uq_order_items_order_sku`` would reject the duplicate at flush time anyway.
* :func:`canonical_request_hash` - it must be stable under a client reordering its
  lines (otherwise a legitimate retry is answered 10011), and it must bind the buyer
  (otherwise a key collision between two customers hands one customer the other's
  order, through the "safe retry" path where nobody looks for an authorisation bug).
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.modules.order.workflow import (
    IDEMPOTENCY_RETENTION,
    OrderLineInput,
    PricingRules,
    canonical_request_hash,
    merge_order_lines,
    order_lock_movement_key,
    order_release_movement_key,
    validate_coupon_input,
)


# ---------------------------------------------------------------------------
# merge_order_lines
# ---------------------------------------------------------------------------
def test_duplicate_skus_are_summed() -> None:
    merged = merge_order_lines([OrderLineInput(3, 1), OrderLineInput(3, 2)])
    assert merged == (OrderLineInput(sku_id=3, quantity=3),)


def test_merging_preserves_first_seen_order() -> None:
    """The "first item" a list row shows is the line the customer added first."""
    merged = merge_order_lines(
        [OrderLineInput(9, 1), OrderLineInput(4, 1), OrderLineInput(9, 2)]
    )
    assert [line.sku_id for line in merged] == [9, 4]
    assert merged[0].quantity == 3


def test_an_empty_selection_is_refused() -> None:
    with pytest.raises(ValidationError):
        merge_order_lines([])


@pytest.mark.parametrize("quantity", [0, -1, -100])
def test_a_non_positive_quantity_is_refused(quantity: int) -> None:
    """A zero or negative line is refused rather than dropped: silently discarding it
    would charge for a cart the customer did not build and never say so."""
    with pytest.raises(ValidationError) as caught:
        merge_order_lines([OrderLineInput(3, quantity)])
    assert caught.value.context["sku_id"] == 3


def test_merging_is_idempotent() -> None:
    once = merge_order_lines([OrderLineInput(3, 1), OrderLineInput(3, 2)])
    assert merge_order_lines(once) == once


# ---------------------------------------------------------------------------
# canonical_request_hash
# ---------------------------------------------------------------------------
def _hash(**overrides) -> str:
    values = {
        "user_id": 7,
        "items": [OrderLineInput(3, 1), OrderLineInput(4, 2)],
        "address_id": 9,
        "coupon_id": None,
        "remark": None,
    }
    values.update(overrides)
    return canonical_request_hash(**values)


def test_the_hash_is_stable_across_calls() -> None:
    assert _hash() == _hash()


def test_the_hash_ignores_the_order_of_the_lines() -> None:
    """A client that reorders two lines is making the same request; answering 10011 to
    that would be a false conflict on a legitimate retry."""
    reordered = _hash(items=[OrderLineInput(4, 2), OrderLineInput(3, 1)])
    assert reordered == _hash()


def test_the_hash_is_a_sha256_hex_digest() -> None:
    digest = _hash()
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_the_hash_binds_the_buyer() -> None:
    """The security property.

    ``idempotency_records`` is unique on ``(scope, idempotency_key)`` and **not** per
    customer, so without the buyer in the hash two customers sending the same key with
    the same basket would share a hash - and the second would be handed the first's
    order. Including the buyer turns that into a 10011 conflict instead.
    """
    assert _hash(user_id=7) != _hash(user_id=8)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("address_id", 10),
        ("coupon_id", 5),
        ("remark", "please hurry"),
        ("items", [OrderLineInput(3, 2), OrderLineInput(4, 2)]),
        ("items", [OrderLineInput(3, 1)]),
    ],
)
def test_a_different_body_hashes_differently(field: str, changed: object) -> None:
    """Same key + different body must be detectable (§14.3, 10011)."""
    assert _hash(**{field: changed}) != _hash()


def test_an_absent_remark_equals_an_empty_one() -> None:
    """§2: ``null`` and absent mean the same thing for an optional free-text field, so
    a client that sends ``""`` on one attempt and omits the field on the retry is
    still making the same request."""
    assert _hash(remark=None) == _hash(remark="")


def test_two_coupons_that_are_both_absent_are_equal() -> None:
    assert _hash(coupon_id=None) == _hash(coupon_id=None)


# ---------------------------------------------------------------------------
# Movement keys (INV-003)
# ---------------------------------------------------------------------------
def test_lock_keys_are_deterministic_and_sku_scoped() -> None:
    first = order_lock_movement_key(user_id=7, client_request_id="abc", sku_id=3)
    assert first == order_lock_movement_key(user_id=7, client_request_id="abc", sku_id=3)
    assert first != order_lock_movement_key(user_id=7, client_request_id="abc", sku_id=4)
    assert first != order_lock_movement_key(user_id=8, client_request_id="abc", sku_id=3)
    assert first == "order-lock:7:abc:3"


def test_release_keys_are_order_no_scoped() -> None:
    assert order_release_movement_key(order_no="NV20260922000001", sku_id=3) == (
        "order-release:NV20260922000001:3"
    )
    assert order_release_movement_key(order_no="NV20260922000001", sku_id=3) != (
        order_release_movement_key(order_no="NV20260922000002", sku_id=3)
    )


def test_a_lock_key_and_a_release_key_never_collide() -> None:
    """They share ``inventory_movements.idempotency_key``'s UNIQUE index. A collision
    would make the release look like a replay of the lock, and the stock would never
    come back."""
    assert order_lock_movement_key(user_id=1, client_request_id="x", sku_id=1) != (
        order_release_movement_key(order_no="x", sku_id=1)
    )


def test_the_retention_window_is_a_positive_timedelta() -> None:
    """Retention, not a lock TTL - a TTL that expires while the work is still running
    lets a second request in, which is the one thing a correctness lock must not do."""
    assert IDEMPOTENCY_RETENTION.total_seconds() > 0


# ---------------------------------------------------------------------------
# PricingRules / coupon input
# ---------------------------------------------------------------------------
def test_no_rules_and_no_coupon_is_the_live_http_path() -> None:
    validate_coupon_input(coupon_id=None, rules=PricingRules())


def test_a_coupon_without_a_resolver_is_refused_loudly() -> None:
    """§14.2: a coupon the customer selected must not vanish silently. Dropping it
    would charge full price for a coupon they chose."""
    from app.core.errors import CouponNotFoundError, ErrorCode

    with pytest.raises(CouponNotFoundError) as caught:
        validate_coupon_input(coupon_id=5, rules=PricingRules())
    assert int(caught.value.code) == int(ErrorCode.COUPON_NOT_FOUND) == 90004
    assert caught.value.status_code == 404


def test_a_matching_resolved_rule_is_accepted() -> None:
    from app.modules.pricing.value_objects import CouponRule

    validate_coupon_input(
        coupon_id=5,
        rules=PricingRules(coupon=CouponRule(coupon_id=5, coupon_type="FIXED_AMOUNT", face_value_amount=100)),
    )


def test_a_mismatched_coupon_rule_is_refused() -> None:
    """Persisting ``orders.coupon_id`` while applying a *different* rule would put the
    order and its discount out of step - invisible until someone explains the amount."""
    from app.modules.pricing.value_objects import CouponRule

    with pytest.raises(ValidationError) as caught:
        validate_coupon_input(
            coupon_id=5,
            rules=PricingRules(
                coupon=CouponRule(coupon_id=6, coupon_type="FIXED_AMOUNT", face_value_amount=100)
            ),
        )
    assert caught.value.context["rule_coupon_id"] == 6
