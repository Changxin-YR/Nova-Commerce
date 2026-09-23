"""Order domain - cart-free ordering, the frozen status machine, and its persistence.

Spec sections 30/34/35/36 and 48. The package is split by responsibility so each
file has one reason to change:

* :mod:`~app.modules.order.enums` - the four status axes and the transition table;
* :mod:`~app.modules.order.models` - ``orders`` / ``order_items`` /
  ``order_status_logs``, and the INV-014 snapshots they carry;
* :mod:`~app.modules.order.repository` - data access only (spec section 18).

There is deliberately no ``app.modules.cart``. Spec sections 29/105 make the cart
a client-side selection plus a server preview, and a server-persisted cart would
create a second place where a price exists - exactly what section 37 forbids.
"""

from app.modules.order.enums import (
    AFTER_SALE_STATUSES,
    FULFILLMENT_STATUSES,
    OPERATOR_TYPES,
    ORDER_STATUS_TRANSITIONS,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
    AfterSaleStatus,
    FulfillmentStatus,
    OperatorType,
    OrderStatus,
    PaymentStatus,
    is_valid_transition,
)
from app.modules.order.models import Order, OrderItem, OrderStatusLog

__all__ = [
    "AFTER_SALE_STATUSES",
    "FULFILLMENT_STATUSES",
    "OPERATOR_TYPES",
    "ORDER_STATUSES",
    "ORDER_STATUS_TRANSITIONS",
    "PAYMENT_STATUSES",
    "AfterSaleStatus",
    "FulfillmentStatus",
    "OperatorType",
    "Order",
    "OrderItem",
    "OrderStatus",
    "OrderStatusLog",
    "PaymentStatus",
    "is_valid_transition",
]
