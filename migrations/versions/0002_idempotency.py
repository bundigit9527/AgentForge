"""Scope client submission keys to a tenant, space and user."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.create_index(
        "uq_runs_scope_idempotency",
        "runs",
        ["tenant_id", "space_id", "user_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_runs_scope_idempotency", table_name="runs")
    op.drop_column("runs", "idempotency_key")
