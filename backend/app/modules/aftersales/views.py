"""Shared read-path assembly for the after-sales HTTP surface.

    detail_view, refunds_for

One helper used by **both** the customer and the console detail handlers, because the
alternative is two implementations of "build the AfterSale detail" that drift. The
symptom would be specific and confusing: the console showing a different
``refund_cap`` from the customer's own view of the same claim, which reads as a
support ticket about a wrong number that is really a second code path.

## The order is read here without a lock, and that is deliberate

The claim has already been scope-checked by the service (``user_id`` for the customer,
``merchant_id`` for the console) before this runs. The order is then read only to
compute a **display** cap - it authorizes nothing, and ``RefundWorkflow`` revalidates
every cap inside its own locks (PHASE5_DESIGN section 8: application checks are not the
boundary). So no lock, and if the order is unreadable the cap falls back to the claim's
own remaining approval: stricter than the truth, which is the safe direction for a cap
to fail in.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.aftersales.models import AfterSale, Refund
from app.modules.aftersales.repository import RefundRepository
from app.modules.aftersales.schemas import AfterSaleDetailOut
from app.modules.aftersales.serializers import to_detail
from app.modules.aftersales.service import AfterSaleService
from app.modules.order.models import OrderItem
from app.modules.order.repository import OrderRepository

__all__ = ["detail_view", "refunds_for"]


def refunds_for(*, session: Session, claim: AfterSale) -> list[Refund]:
    """Every refund movement against one claim, oldest first."""
    return RefundRepository(session).list_for_after_sale(claim.id)


def detail_view(
    *, session: Session, service: AfterSaleService, claim: AfterSale
) -> AfterSaleDetailOut:
    """The full ``AfterSale`` shape for one already-scoped claim.

    Two queries beyond the claim itself: its lines, and the order (whose ``items``
    arrive in one more via ``selectin``). Fixed, not proportional to the number of
    claims on the page - the *summary* shape carries no collections precisely so the
    console queue stays off this path.
    """
    items = service.items_of(claim)
    refunds = refunds_for(session=session, claim=claim)
    order = OrderRepository(session).get(claim.order_id)
    order_items: dict[int, OrderItem] | None = None
    if order is not None:
        order_items = {item.id: item for item in order.items}
    return to_detail(
        claim,
        order=order,
        items=items,
        refunds=refunds,
        order_items=order_items,
    )
