"""Rakazo view of existing resources; no HTTP-to-HTTP forwarding or separate executor."""

import hashlib
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from agentforge_core.models import RunConflict, RunNotFound
from sqlalchemy.exc import IntegrityError

from agentforge_storage.conversations import ConversationRepository, agent_row, scoped, thread_row
from agentforge_storage.identity import insert
from agentforge_storage.model_preferences import ModelPreferences
from agentforge_storage.schema import (
    agents,
    approval_requests,
    messages,
    rpc_bot_profiles,
    rpc_thread_selections,
    runs,
    thread_event_heads,
    thread_events,
    threads,
)
from agentforge_storage.thread_feed import append_thread_event, iso


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

    def thread_id(self, scope, bot_id, *, connection=None):
        if connection is None:
            with self.engine.connect() as conn:
                return self.thread_id(scope, bot_id, connection=conn)
        selected = connection.execute(
            sa.select(rpc_thread_selections.c.thread_id).where(
                scoped(rpc_thread_selections, scope), rpc_thread_selections.c.agent_id == bot_id
            )
        ).scalar_one_or_none()
        return selected or compatibility_id("thread", *asdict(scope).values(), bot_id)

    def _lock_selection(self, conn, scope, bot_id):
        self.policy.require(conn, scope, write=True)
        agent_row(conn, scope, bot_id, authorization=self.policy)
        conn.execute(
            insert(conn, rpc_thread_selections)
            .values(**asdict(scope), agent_id=bot_id)
            .on_conflict_do_nothing()
        )
        conn.execute(
            rpc_thread_selections.update()
            .where(scoped(rpc_thread_selections, scope), rpc_thread_selections.c.agent_id == bot_id)
            .values(thread_id=rpc_thread_selections.c.thread_id)
        )
        return self.thread_id(scope, bot_id, connection=conn)

    def ensure_thread(self, scope, bot_id, *, connection=None):
        if connection is None:
            with self.engine.begin() as conn:
                return self.ensure_thread(scope, bot_id, connection=conn)
        tid = self._lock_selection(connection, scope, bot_id)
        exists = connection.execute(
            sa.select(threads.c.thread_id).where(threads.c.thread_id == tid)
        ).first()
        if exists:
            thread_row(connection, scope, tid, authorization=self.policy, owner_only=True)
        else:
            self.conversations.create_thread(
                scope, bot_id, "Rakazo 会话", _connection=connection, _thread_id=tid
            )
        connection.execute(
            rpc_thread_selections.update()
            .where(scoped(rpc_thread_selections, scope), rpc_thread_selections.c.agent_id == bot_id)
            .values(thread_id=tid)
        )
        return tid

    @staticmethod
    def _expect_thread(tid, expected):
        if expected is not None and tid != expected:
            raise RunConflict("The selected conversation changed; refresh and try again")

    def _lock_idle(self, conn, scope, tid):
        row = thread_row(conn, scope, tid, authorization=self.policy, owner_only=True)
        locked = conn.execute(
            threads.update()
            .where(threads.c.thread_id == tid, threads.c.active_run_id.is_(None))
            .values(updated_at=threads.c.updated_at)
            .returning(threads.c.thread_id)
        ).scalar_one_or_none()
        if locked is None:
            raise RunConflict("Stop current work before changing or clearing the conversation")
        return row

    def conversations_page(self, scope, bot_id, offset=0, limit=50):
        with self.engine.connect() as conn:
            agent = agent_row(conn, scope, bot_id, authorization=self.policy)
            tid = self._read_thread(conn, scope, bot_id)
            current = (
                conn.execute(sa.select(threads).where(threads.c.thread_id == tid))
                .mappings()
                .first()
            )
            rows = (
                conn.execute(
                    sa.select(threads)
                    .where(scoped(threads, scope), threads.c.agent_id == bot_id)
                    .order_by(threads.c.created_at.desc(), threads.c.thread_id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )

            def view(row):
                return {
                    "threadId": row["thread_id"],
                    "title": row["title"],
                    "agentVersion": row["agent_version"],
                    "activeRunId": row["active_run_id"],
                    "createdAt": iso(row["created_at"]),
                }

            return {
                "botId": bot_id,
                "agentVersion": agent["version"],
                "currentThreadId": tid,
                "current": view(current) if current else None,
                "items": [view(row) for row in rows[:limit]],
                "nextOffset": offset + limit if len(rows) > limit else None,
            }

    def change_conversation(self, scope, body, *, create):
        bot_id = body["botId"]
        with self.engine.begin() as conn:
            tid = self.ensure_thread(scope, bot_id, connection=conn)
            target = (
                compatibility_id("new-thread", *asdict(scope).values(), bot_id, body["requestId"])
                if create
                else body["threadId"]
            )
            # Retrying an accepted creation is safe; never silently reselect an older request.
            if create and target == tid:
                row = thread_row(conn, scope, tid, authorization=self.policy, owner_only=True)
                if row["title"] != (body.get("title") or "新会话"):
                    raise RunConflict("Conversation request was already used with another title")
                return {"ok": True, "threadId": tid}
            self._expect_thread(tid, body["expectedThreadId"])
            self._lock_idle(conn, scope, tid)
            if create:
                if conn.execute(
                    sa.select(threads.c.thread_id).where(threads.c.thread_id == target)
                ).first():
                    raise RunConflict("Conversation request was already used")
                self.conversations.create_thread(
                    scope,
                    bot_id,
                    body.get("title") or "新会话",
                    _connection=conn,
                    _thread_id=target,
                )
            else:
                row = thread_row(conn, scope, target, authorization=self.policy, owner_only=True)
                if row["agent_id"] != bot_id:
                    raise RunNotFound(target)
                self._lock_idle(conn, scope, target)
            conn.execute(
                rpc_thread_selections.update()
                .where(
                    scoped(rpc_thread_selections, scope), rpc_thread_selections.c.agent_id == bot_id
                )
                .values(thread_id=target)
            )
        return {"ok": True, "threadId": target}

    def clear(self, scope, bot_id, *, expected_thread_id=None):
        with self.engine.begin() as conn:
            tid = self.ensure_thread(scope, bot_id, connection=conn)
            self._expect_thread(tid, expected_thread_id)
            # Match native stop-and-clear. Acquire Run before Thread, just like the
            # Worker, so completion/cancellation cannot deadlock on inverse locks.
            row = thread_row(conn, scope, tid, authorization=self.policy, owner_only=True)
            if row["active_run_id"]:
                try:
                    self.store.cancel(scope, row["active_run_id"], _connection=conn)
                except RunConflict:
                    # A worker may have completed since the read; the idle check below
                    # also rejects a concurrent REST submission rather than clearing it.
                    pass
            self._lock_idle(conn, scope, tid)
            # Keep the immutable Run/Attempt audit trail. Cleared messages are never replayed
            # or included in future model context; event sequence numbers are not reused.
            conn.execute(messages.delete().where(messages.c.thread_id == tid))
            conn.execute(thread_events.delete().where(thread_events.c.thread_id == tid))
            conn.execute(
                threads.update()
                .where(threads.c.thread_id == tid)
                .values(updated_at=self.store.clock())
            )
            append_thread_event(conn, tid, "thread.cleared", {}, self.store.clock())
        return {"ok": True}

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
                    agents.update()
                    .where(agents.c.agent_id == agent["agent_id"])
                    .values(computer_mode=body.get("computerMode", "team"))
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

    def update_bot(self, scope, body, model_configuration):
        from agentforge_core.execution import ExecutionSnapshot

        bot_id = body["botId"]
        with self.engine.begin() as conn:
            self.policy.require(conn, scope, write=True)
            agent_row(conn, scope, bot_id, authorization=self.policy, owner_only=True)
            conn.execute(
                agents.update()
                .where(agents.c.agent_id == bot_id)
                .values(updated_at=agents.c.updated_at)
            )
            agent = agent_row(conn, scope, bot_id, authorization=self.policy, owner_only=True)
            if body.get("expectedVersion", agent["version"]) != agent["version"]:
                raise RunConflict("Agent version changed")
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
            if "modelProvider" in body:
                requested = (body["modelProvider"], body["modelId"])
                configured = (
                    ModelPreferences(self.store, model_configuration).default(
                        scope, connection=conn
                    )
                    if requested == (None, None)
                    else model_configuration.select(*requested)
                ).snapshot()
                config = ExecutionSnapshot.model_validate(
                    {**config.model_dump(), "model": configured}
                )
            name = body.get("name", agent["name"])
            if name != agent["name"] or config.model_dump(mode="json") != agent["execution_config"]:
                self.conversations.update_agent(
                    scope, bot_id, agent["version"], name, config, _connection=conn
                )
            editable = {
                "name",
                "title",
                "description",
                "instructions",
                "notifyOnFinish",
                "color",
                "pinned",
            }
            profile = {**profile, **{k: v for k, v in body.items() if k in editable}}
            conn.execute(
                insert(conn, rpc_bot_profiles)
                .values(agent_id=bot_id, **asdict(scope), input_hash="", profile=profile)
                .on_conflict_do_update(index_elements=["agent_id"], set_={"profile": profile})
            )
        return self.bot(scope, bot_id)

    def set_computer(self, scope, bot_id, mode):
        with self.engine.begin() as conn:
            self.policy.require(conn, scope, write=True)
            agent_row(conn, scope, bot_id, authorization=self.policy, owner_only=True)
            # Lock the bot row before checking for outstanding work and changing its binding.
            current = conn.execute(
                sa.select(agents.c.computer_mode)
                .where(agents.c.agent_id == bot_id)
                .with_for_update()
            ).scalar_one()
            if current != mode:
                active = conn.execute(
                    sa.select(runs.c.run_id)
                    .where(
                        runs.c.agent_id == bot_id,
                        runs.c.status.in_(("queued", "running", "waiting_input")),
                    )
                    .limit(1)
                ).first()
                if active:
                    raise RunConflict("Stop the bot first")
                conn.execute(
                    agents.update().where(agents.c.agent_id == bot_id).values(computer_mode=mode)
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
            "computerMode": agent["computer_mode"],
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

    def _read_thread(self, conn, scope, bot_id, expected=None):
        agent_row(conn, scope, bot_id, authorization=self.policy)
        tid = self.thread_id(scope, bot_id, connection=conn)
        self._expect_thread(tid, expected)
        row = conn.execute(sa.select(threads).where(threads.c.thread_id == tid)).mappings().first()
        if row:
            self.policy.check_row(conn, scope, "thread", row, owner_only=True)
        return tid

    @staticmethod
    def _head(conn, tid):
        seq = (
            conn.execute(
                sa.select(thread_event_heads.c.sequence).where(
                    thread_event_heads.c.thread_id == tid
                )
            ).scalar_one_or_none()
            or 0
        )
        return {"threadId": tid, "cursor": seq}

    def head(self, scope, bot_id, *, expected_thread_id=None):
        with self.engine.connect() as conn:
            return self._head(conn, self._read_thread(conn, scope, bot_id, expected_thread_id))

    @staticmethod
    def _history(conn, tid, bot_id, before=None, through=None):
        query = sa.select(thread_events).where(
            thread_events.c.thread_id == tid, thread_events.c.event_type == "thread.message.created"
        )
        if before is not None:
            query = query.where(thread_events.c.sequence < before)
        if through is not None:
            query = query.where(thread_events.c.sequence <= through)
        rows = (
            conn.execute(query.order_by(thread_events.c.sequence.desc()).limit(51)).mappings().all()
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
        if items:
            cards = {
                r["message_id"]: r["blocks"]
                for r in conn.execute(
                    sa.select(approval_requests).where(
                        approval_requests.c.message_id.in_([item["id"] for item in items])
                    )
                ).mappings()
            }
            for item in items:
                if item["id"] in cards:
                    item["blocks"] = cards[item["id"]]
        return {
            "threadId": tid,
            "messages": items,
            "olderCursor": items[0]["seq"] if len(rows) > 50 else None,
        }

    def history(self, scope, bot_id, before=None, *, through=None, expected_thread_id=None):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id, expected_thread_id)
            return self._history(conn, tid, bot_id, before, through)

    @staticmethod
    def _live_text(conn, tid, bot_id, row, cursor):
        if not row or row["status"] != "running":
            return []
        updates = (
            conn.execute(
                sa.select(thread_events)
                .where(
                    thread_events.c.thread_id == tid,
                    thread_events.c.run_id == row["run_id"],
                    thread_events.c.event_type == "thread.progress",
                    thread_events.c.sequence <= cursor,
                    thread_events.c.payload["attempt"].as_integer() == row["attempt"],
                )
                .order_by(thread_events.c.sequence)
                .limit(4096)
            )
            .mappings()
            .all()
        )
        text = ""
        for event in updates:
            payload = event["payload"]
            text = payload["text"] if "text" in payload else text + payload.get("delta", "")
        if not text:
            return []
        latest = updates[-1]
        return [
            {
                "id": "progress:" + row["run_id"],
                "threadId": tid,
                "seq": latest["sequence"],
                "role": "bot",
                "blocks": [{"kind": "progress", "text": text}],
                "botId": bot_id,
                "runId": row["run_id"],
                "createdAt": iso(latest["created_at"]),
            }
        ]

    def snapshot(self, scope, bot_id, *, expected_thread_id=None):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id, expected_thread_id)
            head = self._head(conn, tid)
            history = self._history(conn, tid, bot_id, through=head["cursor"])
            row = (
                conn.execute(
                    sa.select(runs)
                    .join(messages, messages.c.run_id == runs.c.run_id)
                    .where(runs.c.thread_id == tid, messages.c.role == "user")
                    .order_by(messages.c.sequence.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            history["messages"].extend(self._live_text(conn, tid, bot_id, row, head["cursor"]))
        run = run_view(row) if row else None
        return {
            **history,
            **head,
            "botId": bot_id,
            "run": run,
            "activeRuns": [run]
            if run and run["status"] in {"queued", "running", "waiting_input"}
            else [],
        }

    def events(self, scope, bot_id, after, limit=100, *, expected_thread_id=None):
        with self.engine.connect() as conn:
            tid = self._read_thread(conn, scope, bot_id, expected_thread_id)
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
                    **({"runId": r["run_id"]} if r["run_id"] is not None else {}),
                    "type": r["event_type"],
                    "payload": r["payload"],
                    "createdAt": iso(r["created_at"]),
                }
                for r in rows
            ]

    def send(self, scope, body, *, expected_thread_id=None, resolve_config=None):
        with self.engine.begin() as conn:
            tid = self.ensure_thread(scope, body["botId"], connection=conn)
            self._expect_thread(tid, expected_thread_id)
            nonce = body.get("clientNonce")
            key = (
                "rpc:" + hashlib.sha256((tid + ":" + nonce).encode()).hexdigest() if nonce else None
            )
            # Keep existing pre-migration nonce receipts usable.
            if nonce:
                legacy = "rpc:" + hashlib.sha256(nonce.encode()).hexdigest()
                if conn.execute(
                    sa.select(runs.c.run_id).where(
                        scoped(runs, scope),
                        runs.c.thread_id == tid,
                        runs.c.idempotency_key == legacy,
                    )
                ).first():
                    key = legacy
            run = self.store.create(
                scope,
                body["text"],
                key,
                thread_id=tid,
                _connection=conn,
                _latest_agent=True,
                _resolve_config=resolve_config,
            )
            seq = conn.execute(
                sa.select(thread_events.c.sequence)
                .where(
                    thread_events.c.run_id == run.run_id,
                    thread_events.c.event_type == "thread.message.created",
                )
                .order_by(thread_events.c.sequence)
                .limit(1)
            ).scalar_one_or_none()
            if seq is None:
                raise RunConflict("This message was cleared; submit a new message")
        return {"taskId": compatibility_id("task", run.run_id), "runId": run.run_id, "seq": seq}

    def stop(self, scope, bot_id, *, expected_thread_id=None):
        with self.engine.connect() as conn:
            self.policy.require(conn, scope, write=True)
            tid = self._read_thread(conn, scope, bot_id, expected_thread_id)
            run_id = conn.execute(
                sa.select(threads.c.active_run_id).where(threads.c.thread_id == tid)
            ).scalar_one_or_none()
        if run_id:
            self.store.cancel(scope, run_id)
        return {"ok": True}
