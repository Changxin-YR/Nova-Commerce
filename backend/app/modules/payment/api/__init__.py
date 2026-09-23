"""``/payments/*`` - the payment surface of API_CONTRACT section 15.

Three sub-routers, wired by ``app/api/v1/router.py`` under the ``/payments`` prefix:

* :mod:`~app.modules.payment.api.customer` - the buyer's own attempts (JWT).
* :mod:`~app.modules.payment.api.callbacks` - the provider's callback
  (**HMAC signature, no JWT**).
* :mod:`~app.modules.payment.api.admin` - the console list.

The callback surface is the only one in this application that authenticates with
something other than a bearer token, and that is the point rather than an exception:
a payment provider cannot hold a user's JWT, and if it could, it would be able to act
as that user everywhere else (REQ-PAY-005). Its authentication model is a signature
over the raw body.

Nothing here is exported eagerly: the router composes the package by importing each
submodule, and an eager import would make a missing submodule during phased delivery
stop the whole package from loading (which the router's defensive ``ModuleNotFoundError``
handling exists to prevent).
"""

__all__: list[str] = []
