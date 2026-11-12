"""Indexed owner/Bot attachment names, preserving existing bytes and revisions."""

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_artifacts", sa.Column("name_key", sa.String(64), nullable=True))
    files = sa.table(
        "chat_artifacts", sa.column("artifact_id"), sa.column("name"), sa.column("name_key")
    )
    conn = op.get_bind()
    # Bounded metadata batches; never load attachment bytes during the backfill.
    after = ""
    while rows := conn.execute(
        sa.select(files.c.artifact_id, files.c.name)
        .where(files.c.artifact_id > after)
        .order_by(files.c.artifact_id)
        .limit(500)
    ).all():
        for artifact_id, name in rows:
            conn.execute(
                files.update()
                .where(files.c.artifact_id == artifact_id)
                .values(name_key=hashlib.sha256(name.lower().encode("utf-8")).hexdigest())
            )
        after = rows[-1].artifact_id
    # No table rebuild: existing revision foreign keys and blobs stay in place on SQLite too.
    op.create_index(
        "ix_chat_artifact_name",
        "chat_artifacts",
        ["tenant_id", "space_id", "user_id", "agent_id", "name_key"],
    )


def downgrade():
    op.drop_index("ix_chat_artifact_name", table_name="chat_artifacts")
    op.drop_column("chat_artifacts", "name_key")
