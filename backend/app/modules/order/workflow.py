"""``CreateOrderWorkflow`` - the transaction body that turns a selection into an order.

Spec references:
    搂27  ``decide inside the lock, not before it`` - the price and the stock check
         both belong **inside** the transaction that writes the order
    搂35  the order and its lines carry full trade snapshots
    搂37  one price authority: the workflow calls :class:`PricingService` and never
         computes a figure itself
    搂38  business inputs only; every amount is recomputed server-side
    搂41  discounts are allocated pro-rata with the remainder to the last line, which
         is what makes INV-006 exact rather than exact-to-the-cent
    搂48  ``idempotency_records`` commits with the order
    搂49  business rows and the (future) outbox row commit together
    搂112 INV-006, INV-014, INV-015
    PHASE4_DESIGN 搂7 (the frozen transaction body)

## The ordering rule, generalised from Phase 3

Phase 3 established ``lock -> re-read -> decide -> write -> append movement``.
This workflow applies the same rule one level up: **the price is computed and the
stock is reserved inside the transaction that writes the order.** Everything that
could make the outcome wrong - whether the SKU is still on sale, what it costs,
whether the last unit is still there - is decided while the row locks are held.

Reserving first and pricing first are both inside the transaction; what matters is
that nothing was decided *before* it.

## Transaction ownership

This workflow **owns** its transaction: it commits exactly once and rolls
everything back on any failure. That is not a convenience. 搂49 requires the
business rows, the idempotency claim and (later) the outbox row to become visible
together, and a partial commit here is a reserved unit of stock with no order -
the single worst state this module can produce.

## Memory of a past mistake

HANDOFF 搂6 records a bug that cost real time on this project and is worth not
repeating: **the ``idempotency_records`` insert must be attempted, not
pre-checked.** ``SELECT``-then-``INSERT`` is a check-then-act race in which two
transactions both find nothing and both proceed. The unique index
``uq_scope_idempotency_key`` is the serialisation point, so
:meth:`IdempotencyRepository.insert_in_progress` attempts the insert and treats the
duplicate-key error as the answer "you lost the race". This module relies on that
and never re-implements it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import get_context
from app.core.errors import (
    CouponNotFoundError,
    IdempotencyInProgressError,
    IdempotencyPayloadMismatchError,
    InternalError,
    OrderAmountMismatchError,
    ProductNotFoundError,
    ProductNotPublishedError,
    SkuNotAvailableError,
    SkuNotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.identity.models import UserAddress
from app.modules.identity.service import AddressService, Principal
from app.modules.inventory.enums import OperatorType as MovementOperatorType, ReferenceType
from app.modules.inventory.service import InventoryService
from app.modules.marketing.service import PromotionService
from app.modules.order.enums import (
    AfterSaleStatus,
    FulfillmentStatus,
    OperatorType as LogOperatorType,
    OrderStatus,
    PaymentStatus,
)
from app.modules.order.models import Order, OrderItem
from app.modules.order.repository import (
    ORDER_CREATE_SCOPE,
    IdempotencyRepository,
    OrderRepository,
    OrderStatusLogRepository,
)
from app.modules.order.schemas import iso_millis
from app.modules.pricing import (
    CouponRule,
    PricedLine,
    PriceSnapshot,
    PricingService,
    PromotionRule,
)
from app.shared.db.base import utc_now
from app.shared.db.models.idempotency import IdempotencyRecord
from app.shared.outbox import (
    OutboxAggregateType,
    OutboxEventType,
    OutboxWriter,
    order_created_payload,
)

logger = get_logger(__name__)

__all__ = [
    "IDEMPOTENCY_RETENTION",
    "CreateOrderResult",
    "CreateOrderWorkflow",
    "OrderLineInput",
    "PricingRules",
    "canonical_request_hash",
    "load_priced_lines",
    "merge_order_lines",
    "order_lock_movement_key",
]


#: How long a claimed key is remembered (spec 搂48 ``expires_at``). Retention, not a
#: lock TTL: the lock is the ``IN_PROGRESS`` status, which only the transaction that
#: set it can clear. 24h is well past any client's retry window and bounds the
#: table's growth; there is no settings key for it yet because Phase 4 is the first
#: writer and a knob nobody has needed to turn is a knob nobody has tested.
IDEMPOTENCY_RETENTION = timedelta(hours=24)

#: Human-readable reason stored on the create status log (搂36).
ORDER_CREATED_REASON = "order created"

#: Truncation bound for ``orders.first_item_name`` - the column is VARCHAR(200).
FIRST_ITEM_NAME_MAX_LENGTH = 200


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OrderLineInput:
    """One requested line: SKU and quantity, and nothing else (搂38).

    There is deliberately no ``unit_price`` field. A price that can travel in the
    request is a price the server can be talked out of, which is why 搂38 calls this
    the single most important rule of the phase.
    """

    sku_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class PricingRules:
    """The resolved promotion/coupon rules to apply.

    An **internal seam** (PHASE4_DESIGN 搂7): the HTTP layer always passes ``None``,
    and tests use it to exercise the allocation arithmetic end-to-end through the
    real transaction. Phase 6's marketing module builds these objects; Phase 4 must
    not import marketing models, and this dataclass is how it avoids needing to.
    """

    promotion: PromotionRule | None = None
    coupon: CouponRule | None = None


@dataclass(slots=True)
class CreateOrderResult:
    """The outcome, with the distinction the caller actually needs.

    ``replayed`` is the whole point: a client that cannot tell "we just created
    this" from "this was already created" has to guess whether its retry was safe,
    and guessing is how duplicate side effects get introduced (搂14.3, INV-015).
    """

    order: Order
    replayed: bool


# ---------------------------------------------------------------------------
# Line handling
# ---------------------------------------------------------------------------
def merge_order_lines(lines: Iterable[OrderLineInput]) -> tuple[OrderLineInput, ...]:
    """Sum duplicate ``sku_id`` lines, preserving first-seen order.

    One order has at most one line per SKU - the database enforces it with
    ``uq_order_items_order_sku`` - so the merge has to happen before pricing rather
    than being discovered at flush time as an integrity error. Merging *before*
    pricing also matters arithmetically: a per-unit promotion applied to two
    separate lines of the same SKU is not guaranteed to equal the same promotion
    applied to the summed quantity, so the merge is a correctness step and not
    housekeeping.

    First-seen order is preserved rather than sorted, so the "first item" shown in a
    list is the line the customer added first.
    """
    merged: dict[int, int] = {}
    for line in lines:
        if line.quantity <= 0:
            raise ValidationError(
                "every order line must have a positive quantity",
                context={"sku_id": line.sku_id, "quantity": line.quantity},
            )
        merged[line.sku_id] = merged.get(line.sku_id, 0) + line.quantity
    if not merged:
        raise ValidationError("an order must contain at least one line")
    return tuple(OrderLineInput(sku_id=sku_id, quantity=qty) for sku_id, qty in merged.items())


def canonical_request_hash(
    *,
    user_id: int,
    items: Sequence[OrderLineInput],
    address_id: int | None,
    coupon_id: int | None,
    remark: str | None,
) -> str:
    """``sha256`` of the canonical business inputs (搂48 ``request_hash``).

    Canonical means: JSON with sorted keys, no insignificant whitespace, lines
    sorted by SKU so that the same basket written two ways hashes identically (a
    client that reorders two lines is making the same request, and answering 10011
    to that would be a false conflict).

    ## Why ``user_id`` is part of the hash

    ``idempotency_records`` is unique on ``(scope, idempotency_key)`` - **not per
    customer**. Without the buyer in the hash, two different customers who happened
    to send the same key with the same basket would produce the same hash, and the
    second one would be handed the first one's order: a cross-customer read
    produced by a "safe retry" path, which is the last place anyone looks for an
    authorisation bug. Including the buyer turns that case into a 10011 conflict.

    ``user_id`` is a business input of the request - it is who is buying - so this
    is canonicalisation, not a smuggled authorisation check.
    """
    payload = json.dumps(
        {
            "address_id": address_id,
            "coupon_id": coupon_id,
            "items": sorted([line.sku_id, line.quantity] for line in items),
            "remark": remark or "",
            "user_id": user_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def order_lock_movement_key(*, user_id: int, client_request_id: str, sku_id: int) -> str:
    """The deterministic key for the ``ORDER_LOCK`` movement (搂7 step 5).

    Deterministic rather than random because ``inventory_movements.idempotency_key``
    is UNIQUE and is the ledger's own duplicate guard (INV-003): the same order
    line can never lock the same stock twice, even if the workflow were somehow
    re-entered. A random key would throw that protection away.
    """
    return f"order-lock:{user_id}:{client_request_id}:{sku_id}"


def order_release_movement_key(*, order_no: str, sku_id: int) -> str:
    """The matching key for the ``ORDER_RELEASE`` movement on cancel (搂8)."""
    return f"order-release:{order_no}:{sku_id}"


def validate_coupon_input(*, coupon_id: int | None, rules: PricingRules) -> None:
    """A coupon the customer selected must never vanish silently (搂14.2).

    Phase 4 has no resolver (marketing lands in Phase 6), so a non-null
    ``coupon_id`` without an internally supplied rule is ``COUPON_NOT_FOUND
    (90004)``. Dropping it would charge the customer full price for a coupon they
    chose, which is worse than an error they can act on.

    Shared with ``OrderService.preview`` on purpose: preview and create must agree
    about whether a coupon is acceptable, or a preview would succeed and the create
    that followed it would fail - the worst possible ordering for a customer.
    """
    if coupon_id is None:
        return
    if rules.coupon is None:
        raise CouponNotFoundError(
            "this coupon could not be resolved",
            context={"coupon_id": coupon_id},
        )
    if rules.coupon.coupon_id != coupon_id:
        # Persisting `orders.coupon_id` while applying a *different* rule would put
        # the order and its discount out of step - a reconciliation defect that is
        # invisible until someone tries to explain the amount.
        raise ValidationError(
            "the supplied coupon rule does not match the requested coupon_id",
            context={"coupon_id": coupon_id, "rule_coupon_id": rules.coupon.coupon_id},
        )


def log_operator_type(principal: Principal) -> str:
    """Who caused a transition, for ``order_status_logs.operator_type`` (搂36).

    Derived from the verified principal, never from the request body (搂70): a caller
    cannot claim to be staff, so the audit trail cannot be forged.
    """
    return LogOperatorType.STAFF.value if principal.is_staff else LogOperatorType.CUSTOMER.value


def movement_operator(principal: Principal) -> tuple[MovementOperatorType, int | None]:
    """Movement attribution for the inventory ledger (spec 搂28).

    The buyer rather than ``SYSTEM`` when a customer placed the order: "who locked
    this stock" is a question asked during a stock investigation, and "a customer's
    order did, and which customer" is strictly more useful than "the system did".
    """
    if principal.is_staff:
        return MovementOperatorType.STAFF, principal.user_id
    return MovementOperatorType.CUSTOMER, principal.user_id


# ---------------------------------------------------------------------------
# Catalogue reads - write path only
# ---------------------------------------------------------------------------
def load_priced_lines(
    session: Session, items: Sequence[OrderLineInput]
) -> tuple[tuple[PricedLine, ...], int]:
    """Resolve requested lines to :class:`PricedLine` snapshots; return them + merchant.

    ## Why this lives here and not in a repository

    The ``catalog`` context has models but no repository yet, and the order context
    must not own one. The query is deliberately narrow and lives next to its only
    two callers (preview and create) so that the invariant it protects is visible
    where it is used: **this is the only place on the order path that reads the live
    catalogue at all.** The read path - everything in ``serializers.py`` - reads
    nothing but ``order_items`` snapshots, which is what INV-014 means in practice.

    A SKU whose product is not ``PUBLISHED``, or that is not ``ACTIVE`` or is soft
    deleted, is refused rather than priced: selling a withdrawn product is a refund
    and an apology, and refusing here is the only cheap moment to do it.
    """
    sku_ids = [line.sku_id for line in items]
    sku_rows = {
        row.id: row
        for row in session.execute(
            select(ProductSku).where(
                ProductSku.id.in_(sku_ids),
                ProductSku.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    }

    missing = [sku_id for sku_id in sku_ids if sku_id not in sku_rows]
    if missing:
        raise SkuNotFoundError(
            "one or more SKUs in this order no longer exist",
            context={"sku_ids": missing},
        )

    product_ids = {row.product_id for row in sku_rows.values()}
    product_rows = {
        row.id: row
        for row in session.execute(
            select(Product).where(
                Product.id.in_(product_ids),
                Product.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    }

    # Primary image per product, in one query rather than one per line. `role` is
    # the frozen discriminator (catalog IMAGE_ROLES); a product with no PRIMARY row
    # simply has no image, which is not an error.
    primary_images = {
        row.product_id: row
        for row in session.execute(
            select(ProductImage)
            .where(
                ProductImage.product_id.in_(product_ids),
                ProductImage.role == "PRIMARY",
            )
            .order_by(ProductImage.product_id.asc(), ProductImage.sort_order.asc(), ProductImage.id.asc())
        )
        .scalars()
        .all()
    }

    prices: list[PricedLine] = []
    merchants: set[int] = set()

    for line in items:
        sku = sku_rows[line.sku_id]
        if sku.status != "ACTIVE":
            raise SkuNotAvailableError(
                f"SKU {sku.sku_no} is not available for sale",
                context={"sku_id": sku.id, "status": sku.status},
            )

        product = product_rows.get(sku.product_id)
        if product is None:
            raise ProductNotFoundError(
                "the product behind this SKU no longer exists",
                context={"sku_id": sku.id, "product_id": sku.product_id},
            )
        if product.status != "PUBLISHED":
            raise ProductNotPublishedError(
                f"product {product.product_no} is not published",
                context={"product_id": product.id, "status": product.status},
            )

        if product.merchant_id is not None:
            merchants.add(product.merchant_id)

        image = primary_images.get(product.id)
        prices.append(
            PricedLine(
                sku_id=sku.id,
                product_id=product.id,
                product_name=product.name,
                sku_name=sku.name,
                # The durable reference is the object key; the URL is left null
                # rather than signing one here. A presigned URL has a TTL, and
                # persisting one onto an immutable snapshot would mean the order's
                # image silently expires 鈥?the correct place to sign is the read
                # path. `image_object_key` is what makes that possible later.
                image_object_key=image.object_key if image is not None else None,
                image_url=None,
                sku_snapshot=sku.attribute_snapshot,
                unit_price=sku.price_amount,
                quantity=line.quantity,
            )
        )

    if len(merchants) > 1:
        # V1 is single-merchant, but a cart spanning merchants is a real modelling
        # error and silently picking one would attribute the order (and its revenue)
        # to an arbitrary tenant.
        raise ValidationError(
            "all lines in one order must belong to the same merchant",
            context={"merchant_ids": sorted(merchants)},
        )

    # merchant_id is nullable on catalog rows; NULL means "unscoped", and an order
    # must be attributable, so it fails rather than writing a NULL tenant.
    merchant_id = next(iter(merchants)) if merchants else None
    if merchant_id is None:
        raise ValidationError("the SKUs in this order are not attached to a merchant")

    return tuple(prices), merchant_id


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------
class CreateOrderWorkflow:
    """Create one order, exactly once, for one idempotency key."""

    def __init__(self, session: Session, pricing: PricingService | None = None) -> None:
        self._session = session
        self._pricing = pricing or PricingService()
        self._orders = OrderRepository(session)
        self._logs = OrderStatusLogRepository(session)
        self._idempotency = IdempotencyRepository(session)
        self._inventory = InventoryService(session)
        self._addresses = AddressService(session)
        #: The outbox seam (step 9). Shares the caller's session on purpose: the
        #: event row must commit with the order, so it is appended to *this*
        #: transaction rather than written after the commit.
        self._outbox = OutboxWriter(session)
        #: The claim taken in step 1, finalised in step 8. ``None`` until then.
        self._record: IdempotencyRecord | None = None

    # -- public ----------------------------------------------------------
    def execute(
        self,
        *,
        principal: Principal,
        items: Sequence[OrderLineInput],
        address_id: int,
        client_request_id: str,
        idempotency_key: str,
        coupon_id: int | None = None,
        remark: str | None = None,
        pricing_rules: PricingRules | None = None,
        trace_id: str | None = None,
    ) -> CreateOrderResult:
        """Create the order, or return the one this key already created.

        On success the transaction has been committed and ``order`` is a persistent
        instance. On any failure nothing at all was written - no order, no items, no
        stock movement, and no idempotency key, so a genuine retry is still allowed.
        """
        if not idempotency_key:
            # Defence in depth: the HTTP layer already refuses a missing header with
            # 10010, but the workflow is also reachable from tests and (later) the
            # tool gateway, and a create with no key is not idempotent by
            # construction.
            from app.core.errors import IdempotencyKeyRequiredError

            raise IdempotencyKeyRequiredError("an Idempotency-Key is required to create an order")

        lines = merge_order_lines(items)
        resolved_trace_id = trace_id if trace_id is not None else get_context().trace_id
        request_hash = canonical_request_hash(
            user_id=principal.user_id,
            items=lines,
            address_id=address_id,
            coupon_id=coupon_id,
            remark=remark,
        )

        try:
            result = self._create(
                principal=principal,
                lines=lines,
                address_id=address_id,
                client_request_id=client_request_id,
                idempotency_key=idempotency_key,
                coupon_id=coupon_id,
                remark=remark,
                pricing_rules=pricing_rules,
                trace_id=resolved_trace_id,
                request_hash=request_hash,
            )
            self._session.commit()
            return result

        except IntegrityError as exc:
            # 搂7's second guard. Only two unique constraints can realistically
            # fire here: `uq_orders_user_client_request` (a client that lost its
            # Idempotency-Key header and retried) and `uq_scope_idempotency_key`
            # (already handled inside `insert_in_progress`, so it never reaches
            # this block). Rather than parse the driver's error string - which is
            # version-specific and would quietly stop matching - we roll back and
            # ask the question the constraint was enforcing: is there already an
            # order for this (user, client_request_id)?
            #
            # The rollback is the *whole* transaction, stock included. That is the
            # point: a half-created order must not leave a unit locked forever.
            self._session.rollback()
            recovered = self._recover_from_client_request_id(
                user_id=principal.user_id,
                client_request_id=client_request_id,
                request_hash=request_hash,
            )
            if recovered is not None:
                return recovered
            logger.warning(
                "order create hit an integrity error that was not a client_request_id collision",
                client_request_id=client_request_id,
                error=str(exc.orig) if getattr(exc, "orig", None) is not None else None,
            )
            raise

        except Exception:
            # Everything else: nothing was written, and a genuine retry with the
            # same key must still be possible (搂48 - "a rolled-back create leaves
            # no key behind").
            self._session.rollback()
            raise

    # -- the transaction body --------------------------------------------
    def _create(
        self,
        *,
        principal: Principal,
        lines: Sequence[OrderLineInput],
        address_id: int,
        client_request_id: str,
        idempotency_key: str,
        coupon_id: int | None,
        remark: str | None,
        pricing_rules: PricingRules | None,
        trace_id: str | None,
        request_hash: str,
    ) -> CreateOrderResult:
        # -- step 1: claim the key (or replay what it already produced) -----
        replay = self._claim_or_replay(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            user_id=principal.user_id,
        )
        if replay is not None:
            return replay

        # -- step 2: the address, snapshotted -------------------------------
        # `AddressService.get` filters by user_id in the query, so a stranger's
        # address id is ADDRESS_NOT_FOUND - never a 403, which would confirm the id
        # exists (搂109 IDOR).
        address = self._addresses.get(user_id=principal.user_id, address_id=address_id)
        address_snapshot = self._snapshot_address(address)

        # -- step 3: business-input validation that needs no I/O ------------
        rules = pricing_rules or PricingRules()
        validate_coupon_input(coupon_id=coupon_id, rules=rules)

        # -- step 4: price (business inputs only, 搂38) ----------------------
        priced_lines, merchant_id = load_priced_lines(self._session, lines)
        promotion_row = None
        if pricing_rules is None:
            promotion_rule, promotion_row = PromotionService(self._session).resolve_for_cart(
                merchant_id=merchant_id,
                sku_ids={line.sku_id for line in lines},
                now=utc_now(),
                for_update=True,
            )
            rules = PricingRules(promotion=promotion_rule)
        cart = self._pricing.calculate_cart_price(
            priced_lines,
            promotion=rules.promotion,
            coupon=rules.coupon,
            shipping_policy=None,
        )
        if promotion_row is not None and cart.promotion_discount_amount > 0:
            promotion_row.used_quota += 1
            self._session.flush()
        # `build_price_snapshot` is the first INV-006 gate: it refuses to return a
        # snapshot whose per-item payables do not re-add to the order payable, and
        # it refuses a non-zero shipping charge for the same reason (搂14.4).
        snapshot = self._pricing.build_price_snapshot(
            cart,
            promotion_ids=(rules.promotion.promotion_id,) if rules.promotion is not None else (),
            coupon_ids=(rules.coupon.coupon_id,) if rules.coupon is not None else (),
            shipping_policy="FREE",
        )

        # -- step 5: persist the order (the real id is needed below) ---------
        # Inserted BEFORE the reservation, and the ordering is load-bearing: the
        # `order.id` this returns is what every ORDER_LOCK movement records as its
        # `reference_id`, which is how INV-007 ("the ledger explains every stock
        # change") stays answerable without decoding an idempotency-key string. The
        # whole step is one transaction, so a failed reservation rolls this row back
        # with it - there is no orphan to protect against.
        order = self._insert_order(
            principal=principal,
            merchant_id=merchant_id,
            client_request_id=client_request_id,
            request_hash=request_hash,
            coupon_id=coupon_id,
            address_id=address_id,
            address_snapshot=address_snapshot,
            remark=remark,
            snapshot=snapshot,
        )

        # -- step 6: reserve the stock, inside the lock ---------------------
        warehouse_id = self._reserve_stock(
            principal=principal,
            lines=lines,
            client_request_id=client_request_id,
            order_id=order.id,
            merchant_id=merchant_id,
        )
        inserted_items = self._insert_items(
            order=order,
            warehouse_id=warehouse_id,
            snapshot=snapshot,
        )
        self._logs.append(
            order=order,
            from_status=None,
            to_status=OrderStatus.PENDING_PAYMENT,
            operator_type=log_operator_type(principal),
            operator_id=principal.user_id,
            reason=ORDER_CREATED_REASON,
            trace_id=trace_id,
        )

        # -- step 7: assert INV-006 and the summary fields, still in the tx -
        self._assert_invariants(order=order, items=inserted_items)

        # -- step 8: finalise the claim with a NON-SENSITIVE snapshot -------
        record = self._record
        if record is None:  # pragma: no cover - step 1 always sets it
            raise InternalError("the order was created without an idempotency claim")
        self._idempotency.mark_completed(
            record,
            resource_type="ORDER",
            resource_id=order.id,
            response_code=0,
            # 搂48: this JSON sits outside the order's own redaction path, so it
            # carries the three facts a replay needs and nothing else - no receiver
            # name, no phone, no address.
            response_snapshot={
                "order_no": order.order_no,
                "payable_amount": order.payable_amount,
                "created_at": iso_millis(order.created_at),
            },
        )

        # -- step 9: the outbox seam ----------------------------------------
        # §49: the event row is appended to *this* transaction, so it commits
        # with the order or not at all. The position is load-bearing - after step
        # 7's INV-006 assertion (nothing is announced that could still turn out to
        # be wrong) and before the single commit() in `execute()`. `enqueue` neither
        # commits nor opens a transaction, and dedups on the aggregate, so a replay
        # that somehow reached here would not queue a second `order.created`.
        self._outbox.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=order.id,
            idempotency_key=idempotency_key,
            payload=order_created_payload(
                order_no=order.order_no,
                payable_amount=order.payable_amount,
                item_count=order.item_count,
            ),
            merchant_id=order.merchant_id,
        )

        logger.info(
            "order created",
            order_no=order.order_no,
            user_id=principal.user_id,
            payable_amount=order.payable_amount,
            item_count=order.item_count,
            replayed=False,
        )
        return CreateOrderResult(order=order, replayed=False)

    # -- step 1 ----------------------------------------------------------
    def _claim_or_replay(
        self, *, idempotency_key: str, request_hash: str, user_id: int
    ) -> CreateOrderResult | None:
        """Claim ``(order:create, key)``; return a replay if the key is already used.

        The insert is **attempted**, never pre-checked - see the module docstring.
        ``insert_in_progress`` returns ``None`` when the unique index rejects the
        insert, which is the database telling us the key is already claimed.
        """
        record = self._idempotency.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            resource_type="ORDER",
            expires_at=utc_now() + IDEMPOTENCY_RETENTION,
        )

        if record is not None:
            self._record = record
            return None

        existing = self._idempotency.get(scope=ORDER_CREATE_SCOPE, idempotency_key=idempotency_key)
        if existing is None:
            # Someone else holds the claim and their transaction has not committed
            # yet, so their row is not visible to us. `IDEMPOTENCY_REQUEST_IN_PROGRESS
            # (10012)` is exactly this situation, and answering "retry" is far better
            # than the alternatives: creating a second order, or blocking a request
            # thread on another customer's transaction.
            #
            # (The claim is verified, not assumed: `session.py:104` sets the session
            # isolation to READ-COMMITTED, so once the winner commits, our blocked INSERT
            # fails on the unique index and the very next read sees their COMPLETED row -
            # which is why the 8-thread same-key test observes 1 create + 7 replays and
            # zero 10012s. This `raise` is therefore the honest answer for the cases
            # where that wait does not apply, not the common path.)
            raise IdempotencyInProgressError(
                "a request with this Idempotency-Key is still in progress",
                context={"idempotency_key": idempotency_key},
            )

        if not existing.is_completed:
            # Unreachable from this workflow: the claim and its completion share one
            # transaction, so a visible claim is always COMPLETED. Refusing is still
            # the right answer for a record some future writer left in flight -
            # a second order is never the safer failure.
            raise IdempotencyInProgressError(
                "this Idempotency-Key is not available for reuse yet",
                context={"idempotency_key": idempotency_key, "status": existing.status},
            )

        if existing.request_hash != request_hash:
            # Same key, different body. Not a retry - a client bug or a reused key,
            # and silently returning the first order would hide it (搂14.3, 10011).
            raise IdempotencyPayloadMismatchError(
                "this Idempotency-Key was already used with a different request body",
                context={"idempotency_key": idempotency_key},
            )

        return CreateOrderResult(
            order=self._load_replayed_order(existing=existing, user_id=user_id),
            replayed=True,
        )

    def _load_replayed_order(self, *, existing: object, user_id: int) -> Order:
        """Load the order a COMPLETED claim refers to, verifying ownership."""
        resource_id = getattr(existing, "resource_id", None)
        order: Order | None = None
        if resource_id is not None:
            order = self._orders.get(int(resource_id))
        if order is None:
            snapshot = getattr(existing, "response_snapshot", None) or {}
            order_no = snapshot.get("order_no")
            if order_no:
                order = self._orders.get_by_order_no(order_no, user_id=user_id)

        if order is None:
            # A COMPLETED claim that cannot name its order is server-side corruption:
            # the two are written in one transaction, so they cannot legitimately
            # disagree. A 500 is the honest answer - replaying "nothing" would look
            # like a successful create to the client.
            raise InternalError(
                "an idempotency record marked COMPLETED refers to an order that cannot be loaded",
                context={"idempotency_key": getattr(existing, "idempotency_key", None)},
            )

        if order.user_id != user_id:
            # Defence in depth. `request_hash` already binds the buyer, so a hash
            # match implies the same buyer; this check means that even a future bug
            # in the hash cannot turn the replay path into a cross-customer read.
            logger.error(
                "idempotency replay attempted across customers",
                order_no=order.order_no,
                order_user_id=order.user_id,
                caller_user_id=user_id,
            )
            raise IdempotencyPayloadMismatchError(
                "this Idempotency-Key was already used with a different request body",
                context={"idempotency_key": getattr(existing, "idempotency_key", None)},
            )
        return order

    def _recover_from_client_request_id(
        self, *, user_id: int, client_request_id: str, request_hash: str
    ) -> CreateOrderResult | None:
        """The second guard (搂7): did a concurrent create already make this order?

        Called *after* a rollback, so the read runs in a fresh transaction and can
        see a row the previous attempt could not. Returns ``None`` when there is no
        such order, which means the integrity error came from somewhere else and the
        original exception must be re-raised rather than swallowed.
        """
        existing = self._orders.get_by_client_request_id(
            user_id=user_id, client_request_id=client_request_id
        )
        if existing is None:
            return None

        if existing.request_hash != request_hash:
            # Same client_request_id, different body: 10011, exactly as for the
            # header guard. The two guards must not disagree about this case.
            raise IdempotencyPayloadMismatchError(
                "this client_request_id was already used with a different request body",
                context={"client_request_id": client_request_id},
            )

        logger.info(
            "order create recovered as a replay via client_request_id",
            order_no=existing.order_no,
            client_request_id=client_request_id,
        )
        return CreateOrderResult(order=existing, replayed=True)

    # -- step 2 helpers ---------------------------------------------------
    @staticmethod
    def _snapshot_address(address: UserAddress) -> dict[str, object]:
        """Freeze the address onto the order (搂35, INV-014).

        Composed as ``UserAddress.to_snapshot()`` **plus** the structured geography:
        ``to_snapshot`` is the Phase-2 helper written for exactly this purpose (it
        returns a plain dict rather than a reference, so a later edit cannot rewrite
        history), and the extra keys give the read path the parts it needs to render
        without re-joining anything.
        """
        snapshot: dict[str, object] = dict(address.to_snapshot())
        for key in ("province", "city", "district", "detail"):
            snapshot[key] = getattr(address, key, "") or ""
        return snapshot

    # -- step 5 ----------------------------------------------------------
    def _reserve_stock(
        self,
        *,
        principal: Principal,
        lines: Sequence[OrderLineInput],
        client_request_id: str,
        order_id: int,
        merchant_id: int,
    ) -> int:
        """Reserve every line, **sorted by sku_id**, and return the warehouse id.

        Sorted because lock ordering is what makes two concurrent multi-line orders
        impossible to deadlock: every transaction acquires the same rows in the same
        order. The reservation itself does ``lock -> re-read -> decide``
        (InventoryService), so availability is decided while the row lock is held and
        never before it (搂27).

        One warehouse for the whole order, resolved once: ``order_items.warehouse_id``
        records "the row that was locked", and splitting one order across warehouses
        would make that column describe nothing.
        """
        warehouse = self._inventory.resolve_warehouse(None, merchant_id=merchant_id)
        operator_type, operator_id = movement_operator(principal)

        for line in sorted(lines, key=lambda entry: entry.sku_id):
            self._inventory.reserve(
                sku_id=line.sku_id,
                quantity=line.quantity,
                idempotency_key=order_lock_movement_key(
                    user_id=principal.user_id,
                    client_request_id=client_request_id,
                    sku_id=line.sku_id,
                ),
                warehouse_id=warehouse.id,
                reference_type=ReferenceType.ORDER,
                # The order row is already inserted (step 5), so the ledger names
                # the order that locked the unit - INV-007 wants a reference_id,
                # not a key a human has to decode.
                reference_id=order_id,
                operator_type=operator_type,
                operator_id=operator_id,
            )
        return warehouse.id

    # -- step 6 ----------------------------------------------------------
    def _insert_order(
        self,
        *,
        principal: Principal,
        merchant_id: int,
        client_request_id: str,
        request_hash: str,
        coupon_id: int | None,
        address_id: int,
        address_snapshot: dict[str, object],
        remark: str | None,
        snapshot: PriceSnapshot,
    ) -> Order:
        """Insert the order with a temporary unique ``order_no``, then stamp the real one.

        ``order_no`` is derived from the auto-increment id (``NV{YYYYMMDD}{id:06d}``),
        so the id has to exist before the identifier can be built. The placeholder is
        a 32-character uuid4 hex - unique against ``uq_orders_merchant_order_no``
        *within the transaction*, and replaced before the commit, so no client ever
        observes it. A random placeholder rather than a sequential one keeps two
        concurrent transactions from colliding on the placeholder itself.
        """
        settings = get_settings()
        now = utc_now()

        order = Order(
            merchant_id=merchant_id,
            user_id=principal.user_id,
            order_no=uuid4().hex,
            client_request_id=client_request_id,
            request_hash=request_hash,
            order_status=OrderStatus.PENDING_PAYMENT.value,
            payment_status=PaymentStatus.UNPAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            after_sale_status=AfterSaleStatus.NONE.value,
            original_amount=snapshot.original_amount,
            promotion_discount_amount=snapshot.promotion_discount_amount,
            coupon_discount_amount=snapshot.coupon_discount_amount,
            shipping_amount=snapshot.shipping_amount,
            payable_amount=snapshot.payable_amount,
            # Nothing is paid yet: paid_amount is Phase 5's to write, and seeding it
            # from `payable_amount` would make INV-005 unfalsifiable.
            paid_amount=0,
            refunded_amount=0,
            coupon_id=coupon_id,
            address_id=address_id,
            receiver_name=str(address_snapshot.get("receiver_name") or ""),
            receiver_phone=str(address_snapshot.get("receiver_phone") or ""),
            address_snapshot=dict(address_snapshot),
            remark=remark,
            item_count=snapshot.item_count,
            first_item_name=self._first_item_name(snapshot),
            expires_at=now + timedelta(minutes=settings.ORDER_PAYMENT_TIMEOUT_MINUTES),
            created_at=now,
            updated_at=now,
        )
        self._orders.add(order)

        # The human-facing identifier, now that the id exists. Same date basis as
        # `created_at` (UTC), so an order created at 23:59:59 does not acquire
        # tomorrow's date.
        order.order_no = f"{settings.ORDER_NO_PREFIX}{now:%Y%m%d}{order.id:06d}"
        self._session.flush()
        return order

    @staticmethod
    def _first_item_name(snapshot: PriceSnapshot) -> str:
        """``product_name`` + ``sku_name`` of the first line, truncated to 200 (搂6).

        A snapshot, not a join: the list column has to be readable from the order
        row alone, and INV-014 forbids resolving it from the live catalogue.
        """
        items = snapshot.items
        if not items:
            return ""
        line = items[0].line
        combined = f"{line.product_name} {line.sku_name}".strip()
        return combined[:FIRST_ITEM_NAME_MAX_LENGTH]

    def _insert_items(self, *, order: Order, warehouse_id: int, snapshot: PriceSnapshot) -> list[OrderItem]:
        """Insert the lines, taking every allocation from the pricing snapshot.

        The per-item split is read through the snapshot's own accessors rather than
        recomputed - 搂41's remainder rule belongs to one function, and a second
        implementation here would be a second answer to "how much of the discount is
        this line's". ``allocated_discount_amount`` is the sum of the two, and
        ``payable_amount`` is ``original - allocated``: both restated as database
        CHECK constraints so the row cannot disagree with the arithmetic.
        """
        inserted: list[OrderItem] = []
        for item_price in snapshot.items:
            line = item_price.line
            sku_id = line.sku_id
            promotion = snapshot.promotion_allocation(sku_id)
            coupon = snapshot.coupon_allocation(sku_id)
            allocated = snapshot.allocated_discount(sku_id)

            item = OrderItem(
                # The relationship rather than a raw `order_id`: assigning it sets the
                # foreign key *and* appends to `order.items`, so the collection the
                # response serializer reads is already consistent. Setting only the FK
                # would leave `order.items` unloaded, and the first read after the
                # commit would quietly issue another query - or, if it had been loaded
                # before the insert, return an empty list.
                order=order,
                warehouse_id=warehouse_id,
                product_id=line.product_id,
                sku_id=sku_id,
                # -- INV-014: these five columns are copies, never references --
                product_name=line.product_name,
                sku_name=line.sku_name,
                image_object_key=line.image_object_key,
                image_url=line.image_url,
                sku_snapshot=line.sku_snapshot,
                # ---------------------------------------------------------------
                unit_price=line.unit_price,
                quantity=line.quantity,
                original_amount=item_price.original_amount,
                promotion_discount_amount=promotion,
                coupon_discount_amount=coupon,
                allocated_discount_amount=allocated,
                payable_amount=snapshot.item_payable_amount(sku_id),
                refunded_amount=0,
                after_sale_status=AfterSaleStatus.NONE.value,
            )
            inserted.append(self._orders.add_item(item))
        return inserted

    # -- step 7 ----------------------------------------------------------
    @staticmethod
    def _assert_invariants(*, order: Order, items: Sequence[OrderItem]) -> None:
        """INV-006 and the summary fields, asserted while a rollback is still cheap.

        搂4.2 is explicit that INV-006 is *not* a CHECK constraint - it spans rows -
        so it is enforced by the allocation algorithm **and** by this assertion,
        executed inside the creating transaction after the flush. Two independent
        mechanisms, because the algorithm is the thing being trusted.

        The failing class is ``ORDER_AMOUNT_MISMATCH (50006)``, a 500: a customer
        whose per-line amounts do not re-add to the total is looking at data that
        must not be persisted, and the honest response is to refuse the write rather
        than to serve a plausible-looking wrong number.
        """
        item_total = sum(item.payable_amount for item in items)
        if item_total != order.payable_amount:
            raise OrderAmountMismatchError(
                "the sum of the order lines does not equal the order payable amount",
                context={
                    "order_payable_amount": order.payable_amount,
                    "items_payable_total": item_total,
                },
            )

        unit_total = sum(item.quantity for item in items)
        if unit_total != order.item_count:
            raise OrderAmountMismatchError(
                "orders.item_count does not equal the total units on its lines",
                context={"item_count": order.item_count, "line_units": unit_total},
            )

        if not order.first_item_name:
            raise OrderAmountMismatchError(
                "orders.first_item_name must name the first line",
                context={"order_no": order.order_no},
            )
