"""Email/password credentials for the unmodified Rakazo Web login."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "web_credentials",
        sa.Column("credential_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("failed_attempts", sa.Integer, nullable=False),
        sa.Column("locked_until", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_web_credential_user"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "space_id", "user_id"],
            [
                "identity_memberships.tenant_id",
                "identity_memberships.space_id",
                "identity_memberships.user_id",
            ],
        ),
    )
    with op.batch_alter_table("browser_sessions") as batch:
        batch.add_column(sa.Column("credential_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_browser_credential", "web_credentials", ["credential_id"], ["credential_id"]
        )


def downgrade():
    # Do not turn password sessions into orphaned valid token sessions after a downgrade.
    op.execute(sa.text("DELETE FROM browser_sessions WHERE credential_id IS NOT NULL"))
    with op.batch_alter_table("browser_sessions") as batch:
        batch.drop_constraint("fk_browser_credential", type_="foreignkey")
        batch.drop_column("credential_id")
    op.drop_table("web_credentials")
