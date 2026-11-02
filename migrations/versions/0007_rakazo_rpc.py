"""Add Rakazo RPC profiles, durable thread events and browser sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
for name, key in (
    ("agents", "agent_id"),
    ("threads", "thread_id"),
    ("runs", "run_id"),
    ("identity_tokens", "token_id"),
):
    sa.Table(name, metadata, sa.Column(key, sa.String(36), primary_key=True))

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


def upgrade():
    for table in (rpc_bot_profiles, thread_event_heads, thread_events, browser_sessions):
        table.create(op.get_bind())
    # Existing messages retain their identities and get a stable thread-wide cursor.
    from datetime import UTC, datetime

    conn = op.get_bind()
    old_messages = sa.table(
        "messages",
        *[
            sa.column(k)
            for k in (
                "message_id",
                "thread_id",
                "run_id",
                "sequence",
                "role",
                "content",
                "created_at",
            )
        ],
    )
    last_tid, seq = None, 0
    for row in conn.execute(
        sa.select(old_messages).order_by(old_messages.c.thread_id, old_messages.c.sequence)
    ).mappings():
        if row["thread_id"] != last_tid:
            if last_tid is not None:
                conn.execute(thread_event_heads.insert().values(thread_id=last_tid, sequence=seq))
            last_tid, seq = row["thread_id"], 0
        seq += 1
        payload = {
            "messageId": row["message_id"],
            "role": "bot" if row["role"] == "assistant" else "user",
            "blocks": [{"kind": "text", "text": row["content"]}],
            "createdAt": datetime.fromtimestamp(row["created_at"], UTC)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        conn.execute(
            thread_events.insert().values(
                thread_id=last_tid,
                sequence=seq,
                run_id=row["run_id"],
                event_type="thread.message.created",
                payload=payload,
                created_at=row["created_at"],
            )
        )
    if last_tid is not None:
        conn.execute(thread_event_heads.insert().values(thread_id=last_tid, sequence=seq))


def downgrade():
    for table in (browser_sessions, thread_events, thread_event_heads, rpc_bot_profiles):
        table.drop(op.get_bind())
