"""Persist scoped, immutable Skill Hub installations."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

skill_installations = sa.Table(
    "skill_installations",
    metadata,
    sa.Column("installation_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("source_id", sa.String(68), nullable=False),
    sa.Column("skill_id", sa.String(64), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("manifest", sa.JSON, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("installed_at", sa.Float, nullable=False),
    sa.UniqueConstraint(
        "tenant_id",
        "space_id",
        "user_id",
        "source_id",
        "skill_id",
        "version",
        name="uq_skill_installation_scope_version",
    ),
)


def upgrade() -> None:
    skill_installations.create(op.get_bind())


def downgrade() -> None:
    op.drop_table("skill_installations")
