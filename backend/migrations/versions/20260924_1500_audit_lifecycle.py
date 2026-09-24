"""Allow append-only audit rows to outlive a hard-deleted tenant row."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260924_1500"
down_revision: str | None = "20260924_1400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_audit_records_merchant_id_merchants", "audit_records", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        "fk_audit_records_merchant_id_merchants",
        "audit_records",
        "merchants",
        ["merchant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
