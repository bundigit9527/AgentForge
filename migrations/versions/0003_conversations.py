"""Versioned agents, conversations, immutable run inputs and attempt history.

Existing runs retain NULL inputs: their original model cannot be inferred safely.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
# Resolve only the pre-existing foreign-key target; this table is never created here.
sa.Table("runs", metadata, sa.Column("run_id", sa.String(36), primary_key=True))

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("agent_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)
sa.Index("ix_agents_scope", agents.c.tenant_id, agents.c.space_id, agents.c.user_id)
agent_versions = sa.Table(
    "agent_versions",
    metadata,
    sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("execution_config", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
threads = sa.Table(
    "threads",
    metadata,
    sa.Column("thread_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("agent_id", sa.String(36), nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("message_seq", sa.Integer, nullable=False),
    sa.Column("active_run_id", sa.String(36)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.ForeignKeyConstraint(
        ["agent_id", "agent_version"], ["agent_versions.agent_id", "agent_versions.version"]
    ),
)
sa.Index("ix_threads_scope", threads.c.tenant_id, threads.c.space_id, threads.c.user_id)

messages = sa.Table(
    "messages",
    metadata,
    sa.Column("message_id", sa.String(36), primary_key=True),
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id"), nullable=False),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("thread_id", "sequence", name="uq_messages_sequence"),
    sa.UniqueConstraint("run_id", "role", name="uq_messages_run_role"),
    sa.CheckConstraint("role IN ('user','assistant')", name="ck_message_role"),
)
attempts = sa.Table(
    "run_attempts",
    metadata,
    sa.Column("attempt_id", sa.String(36), primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error_code", sa.String(64)),
    sa.Column("started_at", sa.Float, nullable=False),
    sa.Column("finished_at", sa.Float),
    sa.UniqueConstraint("run_id", "number", name="uq_attempts_number"),
    sa.CheckConstraint(
        "status IN ('running','succeeded','failed','cancelled','expired')", name="ck_attempt_status"
    ),
)


def upgrade() -> None:
    for table in (agents, agent_versions, threads):
        table.create(op.get_bind())
    for column in (
        sa.Column("thread_id", sa.String(36)),
        sa.Column("agent_id", sa.String(36)),
        sa.Column("agent_version", sa.Integer),
        sa.Column("execution_config", sa.JSON),
        sa.Column("context", sa.JSON),
    ):
        op.add_column("runs", column)
    op.create_index("ix_runs_thread", "runs", ["thread_id", "created_at"])
    messages.create(op.get_bind())
    attempts.create(op.get_bind())


def downgrade() -> None:
    op.drop_table("run_attempts")
    op.drop_table("messages")
    op.drop_index("ix_runs_thread", table_name="runs")
    for name in ("context", "execution_config", "agent_version", "agent_id", "thread_id"):
        op.drop_column("runs", name)
    op.drop_table("threads")
    op.drop_table("agent_versions")
    op.drop_table("agents")
