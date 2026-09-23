"""Fulfillment HTTP surface.

Deliberately empty of a ``router``: ``app/api/v1/router.py`` imports each submodule
(``customer``, ``admin``) and mounts the ``router`` it finds there under
``/fulfillments``. Defining an aggregate router here as well would mount every route
a second time under a second ``operationId``, which REQ-API-005's generated types key
off - and duplicate operation ids break that generation rather than merely looking
untidy.
"""

__all__: list[str] = []
