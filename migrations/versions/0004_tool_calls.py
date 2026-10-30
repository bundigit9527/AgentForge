"""Persist versioned tool execution records without changing existing runs."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("runs", metadata, sa.Column("run_id", sa.String(36), primary_key=True))
sa.Table("run_attempts", metadata, sa.Column("attempt_id", sa.String(36), primary_key=True))

tool_calls = sa.Table(
    "tool_calls",
    metadata,
    sa.Column("tool_call_id", sa.String(36), primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column(
        "attempt_id", sa.String(36), sa.ForeignKey("run_attempts.attempt_id"), nullable=False
    ),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("call_id", sa.String(128), nullable=False),
    sa.Column("name", sa.String(64), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("arguments", sa.JSON, nullable=False),
    sa.Column("arguments_hash", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("output", sa.JSON),
    sa.Column("content", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("started_at", sa.Float, nullable=False),
    sa.Column("finished_at", sa.Float),
    sa.UniqueConstraint("attempt_id", "call_id", name="uq_tool_attempt_call"),
    sa.UniqueConstraint("run_id", "sequence", name="uq_tool_run_sequence"),
    sa.CheckConstraint(
        "status IN ('running','succeeded','failed','abandoned')", name="ck_tool_status"
    ),
)


def upgrade() -> None:
    tool_calls.create(op.get_bind())


def downgrade() -> None:
    op.drop_table("tool_calls")
