"""Immutable file revision families; historical files remain version one."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artifact_revisions",
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("chat_artifacts.artifact_id"),
            primary_key=True,
        ),
        sa.Column(
            "family_id", sa.String(36), sa.ForeignKey("chat_artifacts.artifact_id"), nullable=False
        ),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("chat_artifacts.artifact_id")),
        sa.Column("version", sa.Integer, nullable=False),
        sa.UniqueConstraint("family_id", "version", name="uq_artifact_family_version"),
        sa.CheckConstraint("version >= 1", name="ck_artifact_version"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM artifact_revisions LIMIT 1")).first():
        raise RuntimeError("Export file revision history before downgrade")
    op.drop_table("artifact_revisions")
