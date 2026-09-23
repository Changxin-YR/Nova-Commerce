"""Index the bounded expired-order reconciliation scan.

Revision ID: 48f1e3b5a00c
Revises: 507bb852a092
"""

from collections.abc import Sequence

from alembic import op

revision: str = "48f1e3b5a00c"
down_revision: str | None = "507bb852a092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_orders_status_expires_at", "orders", ["order_status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_status_expires_at", table_name="orders")
