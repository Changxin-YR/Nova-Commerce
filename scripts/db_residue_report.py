"""Read-only residue report: the DB state a measurement is taken on.

Design section 13.5: a test count is only meaningful together with the state of the
database it was measured on, because a failing test skips its teardown and the
left-over rows change later tests (orphaned default warehouses shadow merchant-scoped
resolution). This script touches nothing.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.modules.aftersales.models import AfterSale, Refund
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.identity.models import Merchant, User
from app.modules.order.models import Order, OrderItem
from app.modules.payment.models import Payment, PaymentCallback
from app.shared.db.session import get_session_factory

MODELS = [
    ("merchants", Merchant),
    ("users", User),
    ("orders", Order),
    ("order_items", OrderItem),
    ("payments", Payment),
    ("payment_callbacks", PaymentCallback),
    ("fulfillments", Fulfillment),
    ("fulfillment_items", FulfillmentItem),
    ("after_sales", AfterSale),
    ("refunds", Refund),
]

with get_session_factory()() as session:
    for label, model in MODELS:
        count = session.execute(select(func.count()).select_from(model)).scalar_one()
        print(f"{label:20} {count}")
