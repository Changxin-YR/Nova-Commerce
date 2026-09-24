"""Aggregate router for ``/api/v1``.

Every module contributes one router; this file only composes them. It contains
no logic of its own, which keeps the interface layer thin (spec §17) and makes
the public surface auditable in one place.

**One app, one router.** ``build_api_router()`` returns a fresh aggregate per
application instance. The first version owned a module-global router and appended
to it on every call; since ``create_app()`` legitimately runs more than once in a
process (the test suite builds an HTTP client per test), that registered every
module again on each call - duplicate OpenAPI ``operationId``s (which REQ-API-005
generated types key off) and a route table that grew with the number of tests.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_api_router() -> APIRouter:
    """Compose the ``/api/v1`` surface for **one** application instance.

    Imported lazily and defensively so that a partially-implemented module
    cannot stop the application from starting - during phased delivery it is
    normal for ``identity`` to exist while ``knowledge`` does not yet. Once a
    module is present it is wired in with no further edits here.

    Health is deliberately absent: ``/health/live`` and ``/health/ready`` live
    outside ``/api/v1`` (spec section 130) and the app factory mounts them there.
    Adding them here as well would expose a second, undocumented
    ``/api/v1/health/*`` surface.
    """
    from importlib import import_module

    api_router = APIRouter()

    module_prefixes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("app.modules.identity.api", "/auth", ("public", "users", "addresses", "admin")),
        ("app.modules.catalog.api", "/catalog", ("public", "admin", "images")),
        ("app.modules.inventory.api", "/inventory", ("customer", "admin")),
        ("app.modules.cart.api", "/cart", ("customer",)),
        # ORDER MATTERS: the admin router must be included before the customer
        # router. Both are mounted under the same `/orders` prefix, and the
        # consumer detail route is `GET /orders/{order_no}` -- if it were
        # registered first it would capture `GET /orders/admin` and the console
        # list would 404. Do not "tidy" this back into alphabetical order.
        ("app.modules.order.api", "/orders", ("admin", "customer")),
        ("app.modules.payment.api", "/payments", ("customer", "callbacks", "admin")),
        ("app.modules.fulfillment.api", "/fulfillments", ("customer", "admin")),
        ("app.modules.aftersales.api", "/after-sales", ("customer", "admin", "refunds")),
        ("app.modules.marketing.api", "/marketing", ("coupons", "promotions", "admin")),
        ("app.modules.analytics.api", "/analytics", ("admin",)),
        ("app.modules.knowledge.api", "/knowledge", ("admin", "retrieval")),
        ("app.modules.agent.api", "/agent", ("chat", "runs", "admin")),
        ("app.modules.governance.api", "/governance", ("pending_actions", "tools", "admin")),
        ("app.modules.audit.api", "/audit", ("admin",)),
    )

    for module_path, prefix, submodules in module_prefixes:
        for submodule in submodules:
            try:
                module = import_module(f"{module_path}.{submodule}")
            except ModuleNotFoundError:
                continue
            router = getattr(module, "router", None)
            if router is None:
                continue
            tag = f"{prefix.strip('/')}:{submodule}"
            # ``submodule`` names the FILE; it is deliberately NOT part of the
            # mount path. Climbing into the path would mean rewriting every
            # decorator in this codebase, whose frozen routes are already written
            # against the module prefix: ``order/api/admin.py`` declares
            # ``@router.get("/admin")`` and must answer ``GET /orders/admin``,
            # not ``GET /orders/admin/admin``. So a sub-router owns its own
            # sub-path - ``payment/api/callbacks.py`` spells ``/callbacks/{provider}``
            # - and ``f"{prefix}/{submodule}"`` is the wrong fix.
            #
            # The cost of that convention is that a sub-router whose decorator
            # forgets its sub-path mounts on a path that is *registered and
            # plausible but wrong*: a Phase 5 author's ``@router.post("/{provider}")``
            # answered ``POST /payments/MOCK`` instead of ``POST /payments/callbacks/MOCK``.
            # A route table assertion sees nothing wrong, which is why the frozen-path
            # tests in this repo probe by ANSWER rather than by registration.
            api_router.include_router(router, prefix=prefix, tags=[tag])

    # The frozen product publication verbs live at /products/{id}/... rather
    # than under /catalog. Keep the shared /catalog mount for admin CRUD.
    from app.modules.catalog.api.tasks import router as catalog_tasks

    api_router.include_router(catalog_tasks, tags=["catalog:tasks"])

    return api_router


__all__ = ["build_api_router"]
