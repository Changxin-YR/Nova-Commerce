"""Pytest fixture registration for the shared Phase 5 commerce seed.

The seed itself lives in :mod:`tests.integration.commerce.seed` as a plain module, so
that a test (or a non-pytest caller such as a script or the FG-11 emitter) can import
``Shop``, ``paid_order`` and the read-back helpers without pytest's fixture discovery
being involved at all. This file exists only because pytest looks for fixtures in
``conftest.py`` and nowhere else - so it re-exports the two hooks the seed defines and
nothing more.

Why `seed.shop` rather than a copy of it: a second definition of the fixture is a
second definition of the shop, which is the exact duplication ``PHASE5_DESIGN``
section 12 created this package to prevent.
"""

from __future__ import annotations

from tests.integration.commerce.seed import engine, shop

__all__ = ["engine", "shop"]
