"""Persist the actual Computer scope. Existing desktops remain private."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agents",
        sa.Column("computer_mode", sa.String(12), nullable=False, server_default="dedicated"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM agents WHERE computer_mode = 'team' LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Switch team bots to Private before downgrading")
    op.drop_column("agents", "computer_mode")
