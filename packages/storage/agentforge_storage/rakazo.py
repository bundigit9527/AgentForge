"""Rakazo view of existing resources; no HTTP-to-HTTP forwarding or separate executor."""

import hashlib
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from agentforge_core.models import RunConflict
from sqlalchemy.exc import IntegrityError

from agentforge_storage.conversations import ConversationRepository, agent_row, scoped, thread_row
from agentforge_storage.schema import (
    agents,
    messages,
    rpc_bot_profiles,
    runs,
    thread_event_heads,
    thread_events,
    threads,
)
from agentforge_storage.thread_feed import iso


def compatibility_id(kind, *parts):
    return str(uuid5(NAMESPACE_URL, json.dumps(["agentforge-rakazo-v1", kind, *parts])))


def run_view(row):
    return {
        "id": row["run_id"],
        "botId": row["agent_id"],
        "threadId": row["thread_id"],
        "taskId": compatibility_id("task", row["run_id"]),
        "status": "completed" if row["status"] == "succeeded" else row["status"],
        "trigger": "user",
        "routineId": None,
        "modelProvider": (row["execution_config"] or {}).get("model", {}).get("provider"),
        "modelId": (row["execution_config"] or {}).get("model", {}).get("model"),
        "error": row["error_code"],
        "startedAt": None,
        "completedAt": iso(row["updated_at"])
        if row["status"] in {"succeeded", "failed", "cancelled"}
        else None,
        "createdAt": iso(row["created_at"]),
    }


class RakazoRepository:
    def __init__(self, store):
        self.store = store
        self.engine = store.engine
        self.policy = store.authorization
        self.conversations = ConversationRepository(
            self.engine, store.clock, auth_mode=self.policy.mode
        )

    def thread_id(self, scope, bot_id):
        return compatibility_id("thread", *asdict(scope).values(), bot_id)

    def ensure_thread(self, scope, bot_id, *, connection=None):
        tid = self.thread_id(scope, bot_id)

        def ensure(conn):
            self.policy.require(conn, scope, write=True)
            # Serialize first sends to a shared Agent; the thread remains caller-owned.
            agent_row(conn, scope, bot_id, authorization=self.policy)
            conn.execute(
                agents.update()
                .where(agents.c.agent_id == bot_id)
                .values(updated_at=agents.c.updated_at)
            )
            existing = conn.execute(sa.select(threads).where(threads.c.thread_id == tid)).first()
            if existing:
                thread_row(conn, scope, tid, authorization=self.policy, owner_only=True)
            else:
                self.conversations.create_thread(
                    scope, bot_id, "Rakazo 会话", _connection=conn, _thread_id=tid
                )
            return tid

        if connection is not None:
            return ensure(connection)
        with self.engine.begin() as conn:
            return ensure(conn)

    def create_bot(self, scope, body, config):
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        key = body.get("spawnKey")

        def existing():
            with self.engine.connect() as conn:
                self.policy.require(conn, scope, write=True)
                row = (
                    conn.execute(
                        sa.select(rpc_bot_profiles).where(
                            scoped(rpc_bot_profiles, scope), rpc_bot_profiles.c.spawn_key == key
                        )
                    )
                    .mappings()
                    .first()
                )
                if row:
                    if row["input_hash"] != digest:
                        raise RunConflict("Spawn key was reused")
                    return row["agent_id"]

        if key and (found := existing()):
            return self.bot(scope, found)
        try:
            with self.engine.begin() as conn:
                agent = self.conversations.create_agent(
                    scope, body["name"], config, _connection=conn
                )
                conn.execute(
                    rpc_bot_profiles.insert().values(
                        agent_id=agent["agent_id"],
                        **asdict(scope),
                        spawn_key=key,
                        input_hash=digest,
                        profile=body,
                    )
                )
                self.ensure_thread(scope, agent["agent_id"], connection=conn)
        except IntegrityError:
            if key and (found := existing()):
                return self.bot(scope, found)
            raise
        return self.bot(scope, agent["agent_id"])

    def update_bot(self, scope, body):
        from agentforge_core.execution import ExecutionSnapshot

        bot_id = body["botId"]
        with self.engine.begin() as conn:
            agent = agent_row(conn, scope, bot_id, authorization=self.policy, owner_only=True)
            profile = (
                conn.execute(
                    sa.select(rpc_bot_profiles.c.profile).where(
                        rpc_bot_profiles.c.agent_id == bot_id
                    )
                ).scalar_one_or_none()
                or {}
            )
            config = ExecutionSnapshot.model_validate(agent["execution_config"])
            if "instructions" in body:
                config = config.model_copy(update={"instructions": body["instructions"]})
            self.conversations.update_agent(
                scope,
                bot_id,
                agent["version"],
                body.get("name", agent["name"]),
                config,
                _connection=conn,
            )
            profile = {**profile, **{k: v for k, v in body.items() if k != "botId"}}
            from agentforge_storage.identity import insert

            conn.execute(
                insert(conn, rpc_bot_profiles)
                .values(agent_id=bot_id, **asdict(scope), input_hash="", profile=profile)
                .on_conflict_do_update(index_elements=["agent_id"], set_={"profile": profile})
            )
        return self.bot(scope, bot_id)

    def bot(self, scope, bot_id):
        agent = self.conversations.get_agent(scope, bot_id)
        with self.engine.connect() as conn:
            profile = (
                conn.execute(
                    sa.select(rpc_bot_profiles.c.profile).where(
                        rpc_bot_profiles.c.agent_id == bot_id
                    )
                ).scalar_one_or_none()
                or {}
            )
        snapshot = self.snapshot(scope, bot_id)
        latest = snapshot["run"]
        model = agent["execution_config"]["model"]
        return {
            "id": bot_id,
            "spaceId": scope.space_id,
            "name": agent["name"],
            "title": profile.get("title", ""),
            "description": profile.get("description", ""),
            "instructions": agent["execution_config"]["instructions"],
            "color": profile.get("color") or "#6366f1",
            "notifyOnFinish": profile.get("notifyOnFinish", False),
            "pinned": profile.get("pinned", False),
            "sectionId": None,
            "archivedAt": None,
            "unread": False,
            "parentBotId": None,
            "memoryScope": None,
            "threadId": snapshot["threadId"],
            "preview": "",
            "status": latest["status"] if latest else "idle",
            "runErrorId": latest["id"] if latest and latest["status"] == "failed" else None,
            "computerMode": "team",
            "updatedAt": iso(agent["created_at"]),
            "createdAt": iso(agent["created_at"]),
            "voiceId": None,
            "autoSpeak": False,
            "modelProvider": model["provider"],
            "modelId": model.get("model"),
            "thinkingLevel": None,
            "teamChatAmbientEnabled": False,
            "teamChatRules": "",
            "disabledBuiltinTools": [],
            "webhookConfigured": False,
            "spawnKey": profile.get("spawnKey"),
        }

    def bots(self, scope):
        result, offset = [], 0
        while True:
            page = self.conversations.list_agents(scope, offset, 100)
            result.extend(self.bot(scope, item["agent_id"]) for item in page)
            if len(page) < 100:
                return result
            offset += len(page)

    def _read_thread(self, conn, scope, bot_id):
        # Keep borrowed Agent visibility live even if the caller owns the conversation.
        agent_row(conn, scope, bot_id, authorization=self.policy)
        tid = self.thread_id(scope, bot_id)
        row = conn.execute(sa.select(threads).where(threads.c.thread_id == tid)).mappings().first()
        if row:
            self.policy.check_row(conn, scope, "thread", row, owner_only=True)
        return tid

    def head(self, scope, bot_id):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id)
            seq = (
                conn.execute(
                    sa.select(thread_event_heads.c.sequence).where(
                        thread_event_heads.c.thread_id == tid
                    )
                ).scalar_one_or_none()
                or 0
            )
            return {"threadId": tid, "cursor": seq}

    def history(self, scope, bot_id, before=None, *, through=None):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id)
            query = sa.select(thread_events).where(
                thread_events.c.thread_id == tid,
                thread_events.c.event_type == "thread.message.created",
            )
            if before is not None:
                query = query.where(thread_events.c.sequence < before)
            if through is not None:
                query = query.where(thread_events.c.sequence <= through)
            rows = (
                conn.execute(query.order_by(thread_events.c.sequence.desc()).limit(51))
                .mappings()
                .all()
            )
            items = [
                {
                    "id": r["payload"]["messageId"],
                    "threadId": tid,
                    "seq": r["sequence"],
                    "role": r["payload"]["role"],
                    "blocks": r["payload"]["blocks"],
                    "botId": bot_id,
                    "runId": r["run_id"],
                    "createdAt": r["payload"]["createdAt"],
                }
                for r in reversed(rows[:50])
            ]
            return {
                "threadId": tid,
                "messages": items,
                "olderCursor": items[0]["seq"] if len(rows) > 50 else None,
            }

    def snapshot(self, scope, bot_id):
        head = self.head(scope, bot_id)
        history = self.history(scope, bot_id, through=head["cursor"])
        with self.engine.connect() as conn:
            self._read_thread(conn, scope, bot_id)
            row = (
                conn.execute(
                    sa.select(runs)
                    .join(messages, messages.c.run_id == runs.c.run_id)
                    .where(runs.c.thread_id == head["threadId"], messages.c.role == "user")
                    .order_by(messages.c.sequence.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        run = run_view(row) if row else None
        return {
            **history,
            **head,
            "botId": bot_id,
            "run": run,
            "activeRuns": [run] if run and run["status"] in {"queued", "running"} else [],
        }

    def events(self, scope, bot_id, after, limit=100):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id)
            rows = (
                conn.execute(
                    sa.select(thread_events)
                    .where(thread_events.c.thread_id == tid, thread_events.c.sequence > after)
                    .order_by(thread_events.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return [
                {
                    "id": compatibility_id("event", tid, r["sequence"]),
                    "spaceId": scope.space_id,
                    "threadId": tid,
                    "botId": bot_id,
                    "seq": r["sequence"],
                    "runId": r["run_id"],
                    "type": r["event_type"],
                    "payload": r["payload"],
                    "createdAt": iso(r["created_at"]),
                }
                for r in rows
            ]

    def send(self, scope, body):
        tid = self.ensure_thread(scope, body["botId"])
        nonce = body.get("clientNonce")
        key = "rpc:" + hashlib.sha256(nonce.encode()).hexdigest() if nonce else None
        run = self.store.create(scope, body["text"], key, thread_id=tid)
        with self.engine.connect() as conn:
            seq = conn.execute(
                sa.select(thread_events.c.sequence)
                .where(
                    thread_events.c.run_id == run.run_id,
                    thread_events.c.event_type == "thread.message.created",
                )
                .order_by(thread_events.c.sequence)
                .limit(1)
            ).scalar_one()
        return {"taskId": compatibility_id("task", run.run_id), "runId": run.run_id, "seq": seq}

    def stop(self, scope, bot_id):
        with self.engine.connect() as conn:
            self.policy.require(conn, scope, write=True)
            tid = self._read_thread(conn, scope, bot_id)
            run_id = conn.execute(
                sa.select(threads.c.active_run_id).where(threads.c.thread_id == tid)
            ).scalar_one_or_none()
        if run_id:
            self.store.cancel(scope, run_id)
        return {"ok": True}
