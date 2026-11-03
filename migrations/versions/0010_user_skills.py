"""Owner-private skill assets and immutable recipe revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_skills",
        sa.Column("skill_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("space_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("name_key", sa.String(256)),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "space_id", "user_id", "name_key", name="uq_user_skill_name"
        ),
        sa.CheckConstraint("version >= 1", name="ck_user_skill_version"),
        sa.CheckConstraint(
            "(enabled AND name_key IS NOT NULL) OR (NOT enabled AND name_key IS NULL)",
            name="ck_user_skill_name_state",
        ),
    )
    op.create_table(
        "user_skill_versions",
        sa.Column("version_id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("user_skills.skill_id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("skill_id", "version", name="uq_user_skill_revision"),
        sa.CheckConstraint("version >= 1", name="ck_user_skill_revision"),
    )


def downgrade():
    # Prior binaries cannot read user-source snapshots. Never silently remove the
    # revision records that existing Agent/Run snapshots depend on.
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM user_skill_versions")).scalar_one()
    if count:
        raise RuntimeError("Cannot downgrade while user skill versions exist")
    op.drop_table("user_skill_versions")
    op.drop_table("user_skills")
