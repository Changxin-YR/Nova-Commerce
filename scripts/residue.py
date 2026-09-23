"""Test-residue report and sweep - read-only unless you ask otherwise.

Design section 13.5. Run this **before** a measurement and paste its first line into the
evidence: a test count is only meaningful together with the state of the database it was
measured on.

    python -m scripts.residue              # report only (default, touches nothing)
    python -m scripts.residue --purge      # delete recognised residue, then re-report

`--purge` is required to change anything, deliberately: a cleanup tool that is
destructive by default is how a demo database gets emptied by a mistyped schema name.
It prints the database it is about to touch and asks for confirmation unless `--yes` is
given, because the failure mode is unrecoverable.

Exit status is 0 when clean, 1 when residue was found - so a gate script can gate on it.
"""

from __future__ import annotations

import argparse
import sys

from tests.integration.commerce.residue import (
    purge_orphaned_fixture_callbacks,
    purge_test_residue,
    report_residue,
)

from app.core.config import get_settings
from app.shared.db.session import configure_database, get_session_factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or purge fixture residue.")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete recognised residue (default is report-only)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --purge",
    )
    parser.add_argument(
        "--purge-orphans",
        action="store_true",
        help="also delete fixture-shaped callbacks whose order no longer exists",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    url = str(settings.DATABASE_URL)
    # Never print credentials, but always print enough to know which schema this is.
    safe_url = url.split("@")[-1] if "@" in url else url
    print(f"database: {safe_url}")
    configure_database()

    session = get_session_factory()()
    try:
        before = report_residue(session)
        print(before.render())

        if not args.purge:
            if before.is_clean:
                print("\nOK: no residue. Record this line with any measurement taken now.")
                return 0
            print(f"\nRESIDUE PRESENT: {before.total_residue} row(s). Re-run with --purge to remove.")
            return 1

        if before.is_clean:
            print("\nnothing to purge.")
            return 0

        if not args.yes:
            print(f"\nabout to DELETE {before.total_residue} row(s) from {safe_url}")
            reply = input("type 'purge' to confirm: ").strip()
            if reply != "purge":
                print("aborted - nothing was changed.")
                return 1

        purge_test_residue(session, dry_run=False)
        if args.purge_orphans:
            removed = purge_orphaned_fixture_callbacks(session, dry_run=False)
            print(f"removed {removed} orphaned fixture callback(s)")
        after = report_residue(session)
        print("\nafter purge:")
        print(after.render())
        return 0 if after.is_clean else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
