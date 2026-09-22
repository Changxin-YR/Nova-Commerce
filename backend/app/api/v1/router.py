"""Aggregate router for ``/api/v1``.

Every module contributes one router; this file only composes them. It contains
no logic of its own, which keeps the interface layer thin (spec §17) and makes
the public surface auditable in one place.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health")


def register_module_routers() -> None:
    """Attach each bounded context's router.

    Imported lazily and defensively so that a partially-implemented module
    cannot stop the application from starting - during phased delivery it is
    normal for ``identity`` to exist while ``knowledge`` does not yet. Once a
    module is present it is wired in with no further edits here.
    """
    from importlib import import_module

    module_prefixes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("app.modules.identity.api", "/auth", ("public", "users", "addresses", "admin")),
        ("app.modules.catalog.api", "/catalog", ("public", "admin", "images")),
        ("app.modules.inventory.api", "/inventory", ("customer", "admin")),
        ("app.modules.cart.api", "/cart", ("customer",)),
        ("app.modules.order.api", "/orders", ("customer", "admin")),
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
            api_router.include_router(router, prefix=prefix, tags=[tag])


__all__ = ["api_router", "register_module_routers"]
