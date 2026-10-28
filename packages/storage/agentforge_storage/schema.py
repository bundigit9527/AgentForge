import sqlalchemy as sa

metadata = sa.MetaData()
SCHEMA_REVISION = "0002"

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("prompt", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.String(128)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("attempt_id", sa.String(36)),
    sa.Column("lease_until", sa.Float),
    sa.Column("result", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("event_seq", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "status IN ('queued','running','succeeded','failed','cancelled')", name="ck_run_status"
    ),
    sa.CheckConstraint("attempt >= 0 AND event_seq >= 0", name="ck_run_counters"),
)
sa.Index("ix_runs_scope", runs.c.tenant_id, runs.c.space_id, runs.c.user_id)
sa.Index("ix_runs_claim", runs.c.status, runs.c.lease_until, runs.c.created_at)
sa.Index(
    "uq_runs_scope_idempotency",
    runs.c.tenant_id,
    runs.c.space_id,
    runs.c.user_id,
    runs.c.idempotency_key,
    unique=True,
)

events = sa.Table(
    "run_events",
    metadata,
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), primary_key=True),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("data", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
