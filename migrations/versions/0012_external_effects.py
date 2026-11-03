"""External operation approval and dispatch ledger; no existing run changes."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_effects",
        sa.Column("effect_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("request", sa.JSON, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dispatch_attempt_id", sa.String(36)),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("result", sa.JSON),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("run_id", "operation_key", name="uq_effect_run_operation"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','dispatched','succeeded','failed')",
            name="ck_effect_status",
        ),
    )


def downgrade():
    op.drop_table("external_effects")
