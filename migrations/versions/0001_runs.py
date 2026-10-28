"""Initial durable task queue and event history."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("attempt_id", sa.String(36)),
        sa.Column("lease_until", sa.Float),
        sa.Column("result", sa.Text),
        sa.Column("error_code", sa.String(64)),
        sa.Column("event_seq", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="ck_run_status"
        ),
        sa.CheckConstraint("attempt >= 0 AND event_seq >= 0", name="ck_run_counters"),
    )
    op.create_index("ix_runs_scope", "runs", ["tenant_id", "space_id", "user_id"])
    op.create_index("ix_runs_claim", "runs", ["status", "lease_until", "created_at"])
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), primary_key=True),
        sa.Column("sequence", sa.Integer, primary_key=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_index("ix_runs_claim", table_name="runs")
    op.drop_index("ix_runs_scope", table_name="runs")
    op.drop_table("runs")
