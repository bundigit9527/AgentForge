import sqlalchemy as sa

metadata = sa.MetaData()
SCHEMA_REVISION = "0003"

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("agent_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)
sa.Index("ix_agents_scope", agents.c.tenant_id, agents.c.space_id, agents.c.user_id)
agent_versions = sa.Table(
    "agent_versions",
    metadata,
    sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("execution_config", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
threads = sa.Table(
    "threads",
    metadata,
    sa.Column("thread_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("agent_id", sa.String(36), nullable=False),
    sa.Column("agent_version", sa.Integer, nullable=False),
    sa.Column("title", sa.String(200), nullable=False),
    sa.Column("message_seq", sa.Integer, nullable=False),
    sa.Column("active_run_id", sa.String(36)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.ForeignKeyConstraint(
        ["agent_id", "agent_version"], ["agent_versions.agent_id", "agent_versions.version"]
    ),
)
sa.Index("ix_threads_scope", threads.c.tenant_id, threads.c.space_id, threads.c.user_id)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("prompt", sa.Text, nullable=False),
    sa.Column("thread_id", sa.String(36)),
    sa.Column("agent_id", sa.String(36)),
    sa.Column("agent_version", sa.Integer),
    sa.Column("execution_config", sa.JSON),
    sa.Column("context", sa.JSON),
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

sa.Index("ix_runs_thread", runs.c.thread_id, runs.c.created_at)
messages = sa.Table(
    "messages",
    metadata,
    sa.Column("message_id", sa.String(36), primary_key=True),
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id"), nullable=False),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("thread_id", "sequence", name="uq_messages_sequence"),
    sa.UniqueConstraint("run_id", "role", name="uq_messages_run_role"),
    sa.CheckConstraint("role IN ('user','assistant')", name="ck_message_role"),
)
attempts = sa.Table(
    "run_attempts",
    metadata,
    sa.Column("attempt_id", sa.String(36), primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error_code", sa.String(64)),
    sa.Column("started_at", sa.Float, nullable=False),
    sa.Column("finished_at", sa.Float),
    sa.UniqueConstraint("run_id", "number", name="uq_attempts_number"),
    sa.CheckConstraint(
        "status IN ('running','succeeded','failed','cancelled','expired')", name="ck_attempt_status"
    ),
)
