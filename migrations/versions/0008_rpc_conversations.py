"""Private current-conversation selection and run-independent clear events."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rpc_thread_selections",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id")),
    )
    with op.batch_alter_table("thread_events") as batch:
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=True)


def downgrade():
    op.drop_table("rpc_thread_selections")
    op.execute(sa.text("DELETE FROM thread_events WHERE run_id IS NULL"))
    with op.batch_alter_table("thread_events") as batch:
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=False)
