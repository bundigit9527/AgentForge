"""Durable thread-wide projection; called inside the run/event transaction."""

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentforge_storage.schema import messages, thread_event_heads, thread_events


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def message_view(row):
    return {
        "id": row["message_id"],
        "threadId": row["thread_id"],
        "seq": row["sequence"],
        "role": "bot" if row["role"] == "assistant" else "user",
        "blocks": [{"kind": "text", "text": row["content"]}],
        "runId": row["run_id"],
        "createdAt": iso(row["created_at"]),
    }


def append(connection, run, kind, payload):
    return append_thread_event(
        connection, run["thread_id"], kind, payload, run["updated_at"], run_id=run["run_id"]
    )


def append_thread_event(connection, tid, kind, payload, now, *, run_id=None):
    insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    seq = connection.execute(
        insert(thread_event_heads)
        .values(thread_id=tid, sequence=1)
        .on_conflict_do_update(
            index_elements=["thread_id"], set_={"sequence": thread_event_heads.c.sequence + 1}
        )
        .returning(thread_event_heads.c.sequence)
    ).scalar_one()
    connection.execute(
        thread_events.insert().values(
            thread_id=tid,
            sequence=seq,
            run_id=run_id,
            event_type=kind,
            payload=payload,
            created_at=now,
        )
    )
    return seq


def project(connection, run, event_type, data):
    if not run["thread_id"]:
        return
    if event_type == "model_text":
        append(
            connection,
            run,
            "thread.progress",
            {
                "text" if data["reset"] else "delta": data["text"],
                "streaming": True,
                "attempt": data["attempt"],
            },
        )
        return
    if event_type == "running" and run["attempt"] > 1:
        # A reclaimed attempt must replace the former owner's transient draft.
        append(
            connection,
            run,
            "thread.message.updated",
            {
                "messageId": "progress:" + run["run_id"],
                "role": "bot",
                "blocks": [],
            },
        )
    if event_type in {"queued", "succeeded"}:
        role = "user" if event_type == "queued" else "assistant"
        row = (
            connection.execute(
                sa.select(messages).where(
                    messages.c.run_id == run["run_id"], messages.c.role == role
                )
            )
            .mappings()
            .first()
        )
        if row:
            payload = message_view(row)
            payload["messageId"] = payload.pop("id")
            payload["botId"] = run["agent_id"]
            append(connection, run, "thread.message.created", payload)
    kind = {
        "queued": "run.started",
        "waiting_input": "run.waiting_input",
        "approval_answered": "run.started",
        "running": "run.started",
        "succeeded": "run.completed",
        "failed": "run.failed",
        "cancelled": "run.cancelled",
        "tool_started": "agent.tool.called",
        "tool_completed": "agent.tool.completed",
        "tool_failed": "agent.tool.completed",
        "model_completed": "usage.recorded",
    }.get(event_type)
    if kind:
        payload = {**data, "trigger": "user", "status": run["status"]}
        if run["error_code"]:
            payload["error"] = run["error_code"]
        append(connection, run, kind, payload)
