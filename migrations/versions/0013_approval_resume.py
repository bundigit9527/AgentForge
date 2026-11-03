"""Durable approval checkpoints and native chat cards."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def constraints(up):
    if op.get_bind().dialect.name == "sqlite":
        op.execute("PRAGMA defer_foreign_keys=ON")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_run_status", type_="check")
        states = "'queued','running','succeeded','failed','cancelled'"
        if up:
            states += ",'waiting_input'"
        batch.create_check_constraint("ck_run_status", f"status IN ({states})")
    with op.batch_alter_table("run_attempts") as batch:
        batch.drop_constraint("ck_attempt_status", type_="check")
        states = "'running','succeeded','failed','cancelled','expired'"
        if up:
            states += ",'paused'"
        batch.create_check_constraint("ck_attempt_status", f"status IN ({states})")

    if op.get_bind().dialect.name == "sqlite":
        # Rebuilding a referenced table leaves SQLite's deferred violation counter
        # set even after the identical parent keys have been restored. Validate the
        # actual graph before clearing that counter; foreign_keys stays ON.
        if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("Foreign key validation failed during approval migration")
        op.execute("PRAGMA defer_foreign_keys=OFF")


def upgrade():
    op.add_column("runs", sa.Column("execution_checkpoint", sa.JSON))
    op.add_column(
        "runs", sa.Column("approval_resumes", sa.Integer, nullable=False, server_default="0")
    )
    constraints(True)
    op.create_table(
        "approval_requests",
        sa.Column(
            "effect_id",
            sa.String(36),
            sa.ForeignKey("external_effects.effect_id"),
            primary_key=True,
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False, unique=True),
        sa.Column("blocks", sa.JSON, nullable=False),
        sa.Column("answer", sa.String(16)),
    )
    op.create_table(
        "approval_grants",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("space_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("tool_reference", sa.String(80), primary_key=True),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM runs WHERE status='waiting_input' LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Cancel waiting runs before downgrading")
    op.execute("UPDATE run_attempts SET status='expired' WHERE status='paused'")
    op.drop_table("approval_grants")
    op.drop_table("approval_requests")
    constraints(False)
    op.drop_column("runs", "approval_resumes")
    op.drop_column("runs", "execution_checkpoint")
