"""Order HTTP interface: thin controllers only (spec §17).

Two sub-routers, both mounted under ``/orders``:

* :mod:`~app.modules.order.api.customer` - the consumer surface, frozen by §96.
* :mod:`~app.modules.order.api.admin` - the console surface (§14.6).

``app/api/v1/router.py`` must register **admin before customer**. Both live under
the same prefix, and the consumer detail route ``GET /orders/{order_no}`` would
otherwise capture ``GET /orders/admin`` and turn the console list into a 404.
"""
