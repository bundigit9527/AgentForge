"""Immutable user uploads and result files for bot conversations."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("input_files", sa.JSON, nullable=False, server_default="[]"))
    op.create_table(
        "chat_artifacts",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id")),
        sa.Column("nonce", sa.String(66)),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(280)),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "space_id", "user_id", "nonce", name="uq_chat_artifact_nonce"
        ),
        sa.CheckConstraint(
            "size BETWEEN 1 AND 10485760 AND length(data) = size", name="ck_chat_artifact_size"
        ),
    )


def downgrade():
    if (
        op.get_bind().execute(sa.text("SELECT 1 FROM chat_artifacts LIMIT 1")).first()
        or op.get_bind()
        .execute(sa.text("SELECT 1 FROM runs WHERE json_array_length(input_files) > 0 LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Export chat attachments before downgrade")
    op.drop_table("chat_artifacts")
    op.drop_column("runs", "input_files")
