import sqlalchemy as sa

metadata = sa.MetaData()
SCHEMA_REVISION = "0007"

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

tool_calls = sa.Table(
    "tool_calls",
    metadata,
    sa.Column("tool_call_id", sa.String(36), primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column(
        "attempt_id", sa.String(36), sa.ForeignKey("run_attempts.attempt_id"), nullable=False
    ),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("call_id", sa.String(128), nullable=False),
    sa.Column("name", sa.String(64), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("arguments", sa.JSON, nullable=False),
    sa.Column("arguments_hash", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("output", sa.JSON),
    sa.Column("content", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("started_at", sa.Float, nullable=False),
    sa.Column("finished_at", sa.Float),
    sa.UniqueConstraint("attempt_id", "call_id", name="uq_tool_attempt_call"),
    sa.UniqueConstraint("run_id", "sequence", name="uq_tool_run_sequence"),
    sa.CheckConstraint(
        "status IN ('running','succeeded','failed','abandoned')", name="ck_tool_status"
    ),
)

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

rpc_bot_profiles = sa.Table(
    "rpc_bot_profiles",
    metadata,
    sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("spawn_key", sa.String(120)),
    sa.Column("input_hash", sa.String(64), nullable=False),
    sa.Column("profile", sa.JSON, nullable=False),
    sa.UniqueConstraint("tenant_id", "space_id", "user_id", "spawn_key", name="uq_rpc_spawn"),
)
thread_event_heads = sa.Table(
    "thread_event_heads",
    metadata,
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id"), primary_key=True),
    sa.Column("sequence", sa.Integer, nullable=False),
)
thread_events = sa.Table(
    "thread_events",
    metadata,
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id"), primary_key=True),
    sa.Column("sequence", sa.Integer, primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("event_type", sa.String(40), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
browser_sessions = sa.Table(
    "browser_sessions",
    metadata,
    sa.Column("session_id", sa.String(36), primary_key=True),
    sa.Column("session_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("token_id", sa.String(36), sa.ForeignKey("identity_tokens.token_id"), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("revoked_at", sa.Float),
)

sa.Index("ix_thread_events_run", thread_events.c.run_id, thread_events.c.sequence)
