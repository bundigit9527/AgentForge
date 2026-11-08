"""Encrypted account model connections and space-specific choices."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_connections",
        sa.Column("connection_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary, nullable=False),
        sa.Column("configuration", sa.JSON, nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", "provider", name="uq_model_connection_owner"),
    )
    op.create_table(
        "model_selections",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
    )
    op.create_table(
        "model_backups",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("choices", sa.JSON, nullable=False),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM model_connections LIMIT 1")).first():
        raise RuntimeError("Export and remove model connection records before downgrade")
    op.drop_table("model_backups")
    op.drop_table("model_selections")
    op.drop_table("model_connections")
