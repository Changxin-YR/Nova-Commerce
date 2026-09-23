"""Proof that a *setup* failure cannot leak committed seed rows.

Design section 13.5 / the captain's ``t8``. The defect this pins is specific and easy to
mistake for "teardown already works":

* when a **test** fails, pytest resumes the fixture generator, so a ``try/finally`` around
  ``yield`` runs and cleanup happens. That path was never broken.
* when **setup** fails, the generator is abandoned *before* it yields, so ``finally``
  never executes - and because the seed **commits as it builds**, rows it already wrote
  stay in the database and change every later test.

The fix is ``request.addfinalizer`` registered before the first write, so cleanup runs on
every exit path. These tests exercise it by failing deliberately during setup.

Each case runs a throwaway test file in a **child pytest process**: the only honest way to
prove "setup failed" is to have pytest actually fail a setup, and the only honest way to
prove nothing leaked is to read the database afterwards from the outer process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.shared.db.session import get_session_factory

pytestmark = [pytest.mark.integration]

_REPO_BACKEND = Path(__file__).resolve().parents[3]
#: The throwaway file must live **inside this package**: ``shop`` comes from this
#: package's ``conftest.py`` and pytest resolves fixtures through the conftest chain of
#: the file's own location. A file in ``%TEMP%`` collects fine and then errors with
#: "fixture 'shop' not found" - worth recording, because that failure looks like a broken
#: fixture rather than a misplaced file.
#: A **non-collected** staging directory. Anything pytest collects must not be written
#: here: an earlier version wrote the probe into the package itself, and when the child
#: process was killed before its cleanup the stray `test_nested_*.py` was collected by the
#: next run, spawning another - a self-replicating probe. The `.probe` name keeps pytest
#: away from it (`norecursedirs`-style safety by naming), while the throwaway conftest
#: below still makes `shop` resolvable from the child's own directory.
#: Staged in ``tests/build/``: pytest's default ``norecursedirs`` includes ``.*`` **and**
#: ``build``, so neither collection nor git ever sees the probe. ``build/`` is already in
#: the committed .gitignore, which matters because the evidence emitter refuses a PASS on a
#: dirty tree - a probe directory showing up as untracked would break that check, and
#: editing the shared .gitignore to hide my own scratch is the wrong fix.
_STAGE = Path(__file__).resolve().parents[2] / "build" / "probe_stage"

_CONFTEST = """
from tests.integration.commerce.seed import engine, shop  # noqa: F401
"""


def _run_nested(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    _STAGE.mkdir(parents=True, exist_ok=True)
    (_STAGE / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    target = _STAGE / f"probe_{tmp_path.name}.py"
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(target),
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_BACKEND,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "NV_TEST_MARKER": _RUN_PREFIX},
        )
    finally:
        target.unlink(missing_ok=True)


#: Only rows carrying this prefix belong to this test run. Generated once per outer
#: pytest session and passed to the nested process via the environment, so the assertion
#: is immune to whatever any other pytest process is doing against the shared database.
_RUN_PREFIX = f"res-{uuid.uuid4().hex[:8]}"


def _own_residue() -> int:
    """Count rows whose marker starts with this run's unique prefix."""
    session = get_session_factory()()
    try:
        merchants = session.execute(
            text("SELECT COUNT(*) FROM merchants WHERE code LIKE :p"), {"p": f"{_RUN_PREFIX}%"}
        ).scalar_one()
        users = session.execute(
            text("SELECT COUNT(*) FROM users WHERE username LIKE :p"), {"p": f"%{_RUN_PREFIX}%"}
        ).scalar_one()
        return int(merchants) + int(users)
    finally:
        session.close()


def test_a_setup_failure_does_not_leak_seed_rows(tmp_path: Path) -> None:
    """Fail *after* the shop has committed, then assert the database is unchanged.

    ``residue_probe`` depends on ``shop`` and raises in its own body, so the shop has
    fully committed before the failure. If cleanup lived only in a ``finally`` around the
    shop's ``yield``, those rows would survive; with ``addfinalizer`` they do not.
    """
    before = _own_residue()

    result = _run_nested(
        tmp_path,
        '''
        import pytest

        @pytest.fixture
        def residue_probe(shop):
            raise RuntimeError("deliberate setup failure after the shop committed")

        def test_never_runs(residue_probe):
            raise AssertionError("unreachable")
        ''',
    )

    assert result.returncode != 0, "the nested run was supposed to fail"
    assert "deliberate setup failure" in (result.stdout + result.stderr), result.stdout

    after = _own_residue()
    assert after == before, (
        f"a setup failure leaked {after} row(s) for this run's marker: cleanup must be registered "
        "with addfinalizer, before the first write"
    )


def test_a_plain_test_failure_does_not_leak_either(tmp_path: Path) -> None:
    """The easy case, kept as a control so a regression in either path shows up."""
    before = _own_residue()

    result = _run_nested(
        tmp_path,
        '''
        def test_fails(shop):
            assert shop.merchant_id, "the shop was built"
            raise AssertionError("deliberate test failure")
        ''',
    )

    assert result.returncode != 0
    after = _own_residue()
    assert after == before, f"a test failure leaked {after} row(s) for this run's marker"


def test_the_seed_is_clean_after_a_successful_run(tmp_path: Path) -> None:
    """Control: the fixture must not leak on the happy path either."""
    before = _own_residue()

    result = _run_nested(
        tmp_path,
        '''
        def test_passes(shop):
            assert shop.merchant_id
        ''',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    after = _own_residue()
    assert after == before, f"a passing run leaked {after} row(s) for this run's marker"
