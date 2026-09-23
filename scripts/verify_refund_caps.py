"""Independent re-check of the three refund caps against the LIVE database.

Written because the verifier's meta-control dropped ck_payments_refund_cap with a DDL
statement (implicit commit - a savepoint cannot undo it), then restored it. A claim of
repair is not a repair: this reads the live catalogue directly, and it asserts the
CLAUSE as well as the name, because a constraint re-created with the wrong expression
would satisfy a name-only check.
"""
from __future__ import annotations

from sqlalchemy import text

from app.shared.db.session import get_session_factory

EXPECTED = {
    ("payments", "ck_payments_refund_cap"): "refunded_amount` <= `paid_amount",
    ("orders", "ck_orders_refund_cap"): "refunded_amount` <= `paid_amount",
    ("order_items", "ck_order_items_refund_cap"): "refunded_amount` <= `payable_amount",
}

QUERY = text(
    """
    SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
      FROM information_schema.TABLE_CONSTRAINTS tc
      JOIN information_schema.CHECK_CONSTRAINTS cc
        ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
       AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
     WHERE tc.TABLE_SCHEMA = DATABASE()
       AND tc.CONSTRAINT_TYPE = 'CHECK'
     ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME
    """
)

with get_session_factory()() as session:
    rows = session.execute(QUERY).all()

caps = {(t, n): clause for t, n, clause in rows if "refund_cap" in n}
print("refund_cap constraints found:", len(caps))
ok = True
for key, needle in EXPECTED.items():
    clause = caps.get(key)
    if clause is None:
        print(f"  MISSING {key}")
        ok = False
    elif needle not in clause:
        print(f"  WRONG CLAUSE {key}: {clause!r}")
        ok = False
    else:
        print(f"  OK {key[0]:12} {key[1]:28} {clause}")

print("total CHECK constraints:", sum(1 for _ in rows))
print("VERDICT:", "caps intact and clause-correct" if ok else "CAPS NOT INTACT")
