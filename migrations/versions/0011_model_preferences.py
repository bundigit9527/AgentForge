"""Scoped Web model defaults; contains no gateway credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rpc_model_preferences",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("route_fingerprint", sa.String(64), nullable=False),
    )


def downgrade():
    op.drop_table("rpc_model_preferences")
