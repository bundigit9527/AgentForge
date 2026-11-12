import sqlalchemy as sa

metadata = sa.MetaData()
SCHEMA_REVISION = "0019"

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("agent_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("computer_mode", sa.String(12), nullable=False, server_default="dedicated"),
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
    sa.Column("input_files", sa.JSON, nullable=False, server_default="[]"),
    sa.Column("idempotency_key", sa.String(128)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("attempt_id", sa.String(36)),
    sa.Column("lease_until", sa.Float),
    sa.Column("execution_checkpoint", sa.JSON),
    sa.Column("approval_resumes", sa.Integer, nullable=False, server_default="0"),
    sa.Column("result", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("event_seq", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "status IN ('queued','running','waiting_input','succeeded','failed','cancelled')",
        name="ck_run_status",
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
        "status IN ('running','paused','succeeded','failed','cancelled','expired')",
        name="ck_attempt_status",
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
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=True),
    sa.Column("event_type", sa.String(40), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
web_credentials = sa.Table(
    "web_credentials",
    metadata,
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

browser_sessions = sa.Table(
    "browser_sessions",
    metadata,
    sa.Column("session_id", sa.String(36), primary_key=True),
    sa.Column("session_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("credential_id", sa.String(36), sa.ForeignKey("web_credentials.credential_id")),
    sa.Column("token_id", sa.String(36), sa.ForeignKey("identity_tokens.token_id"), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("revoked_at", sa.Float),
)

sa.Index("ix_thread_events_run", thread_events.c.run_id, thread_events.c.sequence)

# Selection is private to the caller, including when an Agent is shared.
rpc_thread_selections = sa.Table(
    "rpc_thread_selections",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), primary_key=True),
    sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.thread_id")),
)

user_skills = sa.Table(
    "user_skills",
    metadata,
    sa.Column("skill_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("name_key", sa.String(256)),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("tenant_id", "space_id", "user_id", "name_key", name="uq_user_skill_name"),
    sa.CheckConstraint("version >= 1", name="ck_user_skill_version"),
    sa.CheckConstraint(
        "(enabled AND name_key IS NOT NULL) OR (NOT enabled AND name_key IS NULL)",
        name="ck_user_skill_name_state",
    ),
)

user_skill_versions = sa.Table(
    "user_skill_versions",
    metadata,
    sa.Column("version_id", sa.String(36), primary_key=True),
    sa.Column("skill_id", sa.String(36), sa.ForeignKey("user_skills.skill_id"), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("manifest", sa.JSON, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.UniqueConstraint("skill_id", "version", name="uq_user_skill_revision"),
    sa.CheckConstraint("version >= 1", name="ck_user_skill_revision"),
)


rpc_model_preferences = sa.Table(
    "rpc_model_preferences",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("route_fingerprint", sa.String(64), nullable=False),
)

external_effects = sa.Table(
    "external_effects",
    metadata,
    sa.Column("effect_id", sa.String(36), primary_key=True),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("operation_key", sa.String(128), nullable=False),
    sa.Column("request", sa.JSON, nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("dispatch_attempt_id", sa.String(36)),
    sa.Column("reviewed_by", sa.String(128)),
    sa.Column("result", sa.JSON),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("run_id", "operation_key", name="uq_effect_run_operation"),
    sa.CheckConstraint(
        "status IN ('pending','approved','rejected','dispatched','succeeded','failed')",
        name="ck_effect_status",
    ),
)

approval_requests = sa.Table(
    "approval_requests",
    metadata,
    sa.Column(
        "effect_id", sa.String(36), sa.ForeignKey("external_effects.effect_id"), primary_key=True
    ),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("message_id", sa.String(36), nullable=False, unique=True),
    sa.Column("blocks", sa.JSON, nullable=False),
    sa.Column("answer", sa.String(16)),
)
approval_grants = sa.Table(
    "approval_grants",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("tool_reference", sa.String(80), primary_key=True),
)

artifacts = sa.Table(
    "run_artifacts",
    metadata,
    sa.Column(
        "artifact_id", sa.String(36), sa.ForeignKey("external_effects.effect_id"), primary_key=True
    ),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), nullable=False, index=True),
    sa.Column("data", sa.LargeBinary(512), nullable=False),
    sa.CheckConstraint("length(data) BETWEEN 1 AND 512", name="ck_run_artifacts_size"),
    sa.Column("created_at", sa.Float, nullable=False),
)

chat_artifacts = sa.Table(
    "chat_artifacts",
    metadata,
    sa.Column("artifact_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("space_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.agent_id"), nullable=False),
    sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id")),
    sa.Column("nonce", sa.String(66)),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("name_key", sa.String(64)),
    sa.Column("description", sa.String(280)),
    sa.Column("mime_type", sa.String(128), nullable=False),
    sa.Column("size", sa.Integer, nullable=False),
    sa.Column("data", sa.LargeBinary, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("tenant_id", "space_id", "user_id", "nonce", name="uq_chat_artifact_nonce"),
    sa.Index("ix_chat_artifact_name", "tenant_id", "space_id", "user_id", "agent_id", "name_key"),
    sa.CheckConstraint(
        "size BETWEEN 1 AND 10485760 AND length(data) = size", name="ck_chat_artifact_size"
    ),
)


artifact_revisions = sa.Table(
    "artifact_revisions",
    metadata,
    sa.Column(
        "artifact_id", sa.String(36), sa.ForeignKey("chat_artifacts.artifact_id"), primary_key=True
    ),
    sa.Column(
        "family_id", sa.String(36), sa.ForeignKey("chat_artifacts.artifact_id"), nullable=False
    ),
    sa.Column("parent_id", sa.String(36), sa.ForeignKey("chat_artifacts.artifact_id")),
    sa.Column("version", sa.Integer, nullable=False),
    sa.UniqueConstraint("family_id", "version", name="uq_artifact_family_version"),
    sa.CheckConstraint("version >= 1", name="ck_artifact_version"),
)


model_connections = sa.Table(
    "model_connections",
    metadata,
    sa.Column("connection_id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("provider", sa.String(128), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("encrypted_key", sa.LargeBinary, nullable=False),
    sa.Column("configuration", sa.JSON, nullable=False),
    sa.UniqueConstraint("tenant_id", "user_id", "provider", name="uq_model_connection_owner"),
)
model_selections = sa.Table(
    "model_selections",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("provider", sa.String(128), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
)
model_backups = sa.Table(
    "model_backups",
    metadata,
    sa.Column("tenant_id", sa.String(128), primary_key=True),
    sa.Column("space_id", sa.String(128), primary_key=True),
    sa.Column("user_id", sa.String(128), primary_key=True),
    sa.Column("choices", sa.JSON, nullable=False),
)
