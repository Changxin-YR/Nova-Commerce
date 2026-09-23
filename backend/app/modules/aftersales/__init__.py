"""After-sales domain - claims, refunds, and the two caps that bound them.

PHASE5_DESIGN sections 3, 4.3, 5.5, 6.2, 8. Import surface only; the use cases live
in :mod:`app.modules.aftersales.service`, the transaction body in
:mod:`~app.modules.aftersales.workflow`, the per-line placement in
:mod:`~app.modules.aftersales.refunds`.

The shape worth remembering before reading any of it: **a claim is a business fact
and a refund is a money fact.** They live in separate tables because they become true
at different times, and the order's ``after_sale_status`` is a third thing again -
a rollup that only the money path may move.
"""

from app.modules.aftersales.enums import (
    AFTER_SALE_CLAIM_STATUSES,
    AFTER_SALE_TYPES,
    REFUND_STATUSES,
    AfterSaleClaimStatus,
    AfterSaleType,
    RefundStatus,
)
from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund

__all__ = [
    "AFTER_SALE_CLAIM_STATUSES",
    "AFTER_SALE_TYPES",
    "REFUND_STATUSES",
    "AfterSale",
    "AfterSaleClaimStatus",
    "AfterSaleItem",
    "AfterSaleType",
    "Refund",
    "RefundStatus",
]
