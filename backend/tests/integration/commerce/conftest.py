"""Pytest fixture registration for the shared Phase 5 commerce seed.

The seed itself lives in :mod:`tests.integration.commerce.seed` as a plain module, so
that a test (or a non-pytest caller such as a script or the FG-11 emitter) can import
``Shop``, ``paid_order`` and the read-back helpers without pytest's fixture discovery
being involved at all. This file exists because pytest looks for fixtures in
``conftest.py`` and nowhere else - so it re-exports the hooks the seed defines and adds
the residue safety net below.

Why `seed.shop` rather than a copy of it: a second definition of the fixture is a second
definition of the shop, which is the exact duplication ``PHASE5_DESIGN`` section 12
created this package to prevent.
"""

from __future__ import annotations

import pytest

from app.shared.db.session import get_session_factory
from tests.integration.commerce.residue import purge_test_residue, report_residue
from tests.integration.commerce.seed import engine, shop

__all__ = ["engine", "residue_safety_net", "shop"]


@pytest.fixture(scope="session", autouse=True)
def residue_safety_net():
    """Sweep marker-attributable residue at the end of the session. Safety net only.

    Design section 13.5. The *correct* fix is per-fixture: register cleanup with
    ``request.addfinalizer`` **before the first write**, so it runs when setup fails
    part-way - which is the one case a ``try/finally`` around ``yield`` cannot cover,
    because an abandoned generator never reaches its ``finally``. ``commerce/seed.py``
    does that.

    Fixtures owned by other modules cannot be fixed from here, so this net exists to stop
    their residue poisoning *this* measurement. It is scoped, not blanket: it deletes only
    rows the sweep can attribute to a fixture marker (merchant/user ids, marker-prefixed
    idempotency keys, marker-shaped event ids), never a table, and never by a loose name
    pattern. See :mod:`tests.integration.commerce.residue` for the attribution rules.

    It also prints the before/after counts, because section 13.5's other half is that a
    test count is only meaningful together with the state it was measured on - a silent
    sweep would hide exactly the information the artifact needs.
    """
    yield
    factory = get_session_factory()
    with factory() as session:
        before = report_residue(session)
        if before.is_clean:
            print("\n[residue] 0 rows attributable to a fixture marker - nothing to sweep")
            return
        print(f"\n[residue] sweeping {before.total_residue} row(s) left by fixtures that "
              "do not yet register addfinalizer:")
        purge_test_residue(session, dry_run=False)
        after = report_residue(session)
        print(f"[residue] {after.line()}")
        if not after.is_clean:
            print("[residue] WARNING: residue remains after a sweep - report it")
