"""Small immutable CSV reports, one per approved operation."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_artifacts",
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("external_effects.effect_id"),
            primary_key=True,
        ),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False, index=True
        ),
        sa.Column("data", sa.LargeBinary(512), nullable=False),
        sa.CheckConstraint("length(data) BETWEEN 1 AND 512", name="ck_run_artifacts_size"),
        sa.Column("created_at", sa.Float, nullable=False),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM run_artifacts LIMIT 1")).first():
        raise RuntimeError("Export and remove existing artifacts before downgrading")
    op.drop_table("run_artifacts")
