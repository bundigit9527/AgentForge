"""Add identity, revocable credentials and explicit space sharing; preserve existing resources."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

identity_tenants = sa.Table(
    "identity_tenants",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
)
identity_spaces = sa.Table(
    "identity_spaces",
    metadata,
    sa.Column(
        "tenant_id", sa.String(128), sa.ForeignKey("identity_tenants.tenant_id"), primary_key=True
    ),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
)
identity_users = sa.Table(
    "identity_users",
    metadata,
    sa.Column(
        "tenant_id", sa.String(128), sa.ForeignKey("identity_tenants.tenant_id"), primary_key=True
    ),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("display_name", sa.String(128), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
)
identity_memberships = sa.Table(
    "identity_memberships",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.ForeignKeyConstraint(
        ["tenant_id", "space_id"], ["identity_spaces.tenant_id", "identity_spaces.space_id"]
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "user_id"], ["identity_users.tenant_id", "identity_users.user_id"]
    ),
    sa.CheckConstraint("role IN ('admin','member','viewer')", name="ck_membership_role"),
)
identity_tokens = sa.Table(
    "identity_tokens",
    metadata,
    sa.Column("token_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("label", sa.String(128), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("revoked_at", sa.Float),
    sa.ForeignKeyConstraint(
        ["tenant_id", "space_id", "user_id"],
        [
            "identity_memberships.tenant_id",
            "identity_memberships.space_id",
            "identity_memberships.user_id",
        ],
    ),
)
resource_shares = sa.Table(
    "resource_shares",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("resource_kind", sa.String(16), primary_key=True),
    sa.Column("resource_id", sa.String(36), primary_key=True),
    sa.CheckConstraint(
        "resource_kind IN ('agent','skill','thread','run')", name="ck_shared_resource_kind"
    ),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
