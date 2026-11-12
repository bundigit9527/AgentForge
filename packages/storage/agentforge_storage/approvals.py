"""Approval pause/resume shares the Run lock and native thread event transaction."""

import json
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.models import RunConflict, RunNotFound

from agentforge_storage.effects import ExternalEffectRepository
from agentforge_storage.identity import insert
from agentforge_storage.repository import _event, _run
from agentforge_storage.schema import (
    approval_grants,
    approval_requests,
    attempts,
    runs,
)
from agentforge_storage.thread_feed import append, iso


class ApprovalRepository:
    def __init__(self, repository):
        self.repo = repository
        self.effects = ExternalEffectRepository(repository)

    def load(self, claim):
        with self.repo.engine.begin() as conn:
            row = self.effects._lock_claim(conn, claim)
            return row["execution_checkpoint"]

    def save(self, claim, state):
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        if len(payload) > 500000:
            raise RunConflict("Execution checkpoint exceeds limit")
        with self.repo.engine.begin() as conn:
            self.effects._lock_claim(conn, claim)
            conn.execute(
                runs.update()
                .where(runs.c.run_id == claim.run.run_id)
                .values(execution_checkpoint=json.loads(payload))
            )

    def granted(self, scope, reference):
        with self.repo.engine.connect() as conn:
            return (
                conn.execute(
                    sa.select(approval_grants).where(
                        approval_grants.c.tenant_id == scope.tenant_id,
                        approval_grants.c.space_id == scope.space_id,
                        approval_grants.c.user_id == scope.user_id,
                        approval_grants.c.tool_reference == reference,
                    )
                ).first()
                is not None
            )

    def grants(self, scope, *, remove=None):
        from agentforge_storage.authorization import owned

        with self.repo.engine.begin() as conn:
            self.repo.authorization.require(conn, scope, write=remove is not None)
            if remove is not None:
                conn.execute(
                    approval_grants.delete().where(
                        owned(approval_grants, scope), approval_grants.c.tool_reference == remove
                    )
                )
            return list(
                conn.execute(
                    sa.select(approval_grants.c.tool_reference)
                    .where(owned(approval_grants, scope))
                    .order_by(approval_grants.c.tool_reference)
                ).scalars()
            )

    def pause(self, claim, effect):
        with self.repo.engine.begin() as conn:
            row = self.effects._lock_claim(conn, claim)
            current = self.effects._find(conn, row["run_id"], effect["effect_id"])
            if current["status"] != "pending":
                return False
            if row["execution_checkpoint"] is None:
                raise RunConflict("Missing execution checkpoint")
            card = (
                conn.execute(
                    sa.select(approval_requests).where(
                        approval_requests.c.effect_id == effect["effect_id"]
                    )
                )
                .mappings()
                .first()
            )
            if card is None:
                mid = str(uuid4())
                request = current["request"]
                blocks = [
                    {
                        "kind": "ask",
                        "approvalEffectId": effect["effect_id"],
                        "text": (
                            "保存附件到团队电脑（同空间成员可见）"
                            if request["arguments"].get("computer_mode") == "team"
                            else "保存附件到私人电脑"
                        )
                        if request["tool_reference"] == "workspace_save@1"
                        else (
                            "写入团队电脑文件（同空间成员可见，按原文件摘要核对覆盖）"
                            if request["arguments"].get("computer_mode") == "team"
                            else "写入私人电脑文件（按原文件摘要核对覆盖）"
                        )
                        if request["tool_reference"] in {"write_file@1", "write_file@2"}
                        else "请确认调用工具：" + request["tool_reference"],
                        "detail": json.dumps(request, ensure_ascii=False, indent=2),
                        "status": "pending",
                        "actions": [
                            {"id": "allow", "label": "允许本次"},
                            {"id": "always", "label": "始终允许此工具"},
                            {"id": "deny", "label": "拒绝"},
                        ],
                    }
                ]
                conn.execute(
                    approval_requests.insert().values(
                        effect_id=effect["effect_id"],
                        run_id=row["run_id"],
                        message_id=mid,
                        blocks=blocks,
                    )
                )
                if row["thread_id"]:
                    append(
                        conn,
                        row,
                        "thread.message.created",
                        {
                            "messageId": mid,
                            "role": "bot",
                            "botId": row["agent_id"],
                            "blocks": blocks,
                            "createdAt": iso(self.repo.clock()),
                        },
                    )
            paused = (
                conn.execute(
                    runs.update()
                    .where(runs.c.run_id == row["run_id"])
                    .values(
                        status="waiting_input",
                        attempt_id=None,
                        lease_until=None,
                        event_seq=runs.c.event_seq + 1,
                        updated_at=self.repo.clock(),
                    )
                    .returning(runs)
                )
                .mappings()
                .one()
            )
            conn.execute(
                attempts.update()
                .where(attempts.c.attempt_id == claim.attempt_id)
                .values(status="paused", finished_at=self.repo.clock())
            )
            _event(conn, paused, "waiting_input", {"effect_id": effect["effect_id"]})
            return True

    def answer(self, scope, run_id, message_id, answer, *, bot_id=None, thread_id=None):
        if answer not in {"allow", "always", "deny"}:
            raise RunConflict("Invalid approval answer")
        with self.repo.engine.begin() as conn:
            # Match the existing cancellation/completion lock order: Run then Thread events.
            self.repo.authorization.require(conn, scope, write=True)
            from agentforge_storage.authorization import owned

            row = (
                conn.execute(
                    runs.update()
                    .where(runs.c.run_id == run_id, owned(runs, scope))
                    .values(updated_at=self.repo.clock())
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            self.repo.authorization.check_row(conn, scope, "run", row, owner_only=True)
            if (bot_id and row["agent_id"] != bot_id) or (
                thread_id and row["thread_id"] != thread_id
            ):
                raise RunNotFound()
            card = (
                conn.execute(
                    sa.select(approval_requests).where(
                        approval_requests.c.run_id == run_id,
                        approval_requests.c.message_id == message_id,
                    )
                )
                .mappings()
                .first()
            )
            if card is None:
                raise RunNotFound()
            if not conn.execute(
                sa.select(self.repo.authorization.execution_condition(_run(row)))
            ).scalar_one():
                raise RunNotFound()
            if card["answer"] is not None:
                if card["answer"] != answer:
                    raise RunConflict("Approval already answered differently")
                return {"ok": True}
            if row["status"] != "waiting_input":
                raise RunConflict("Run is no longer waiting for approval")
            effect = self.effects._find(conn, run_id, card["effect_id"])
            self.effects.decide(
                scope,
                run_id,
                card["effect_id"],
                decision="reject" if answer == "deny" else "approve",
                request_hash=effect["request_hash"],
                _connection=conn,
            )
            if answer == "always":
                conn.execute(
                    insert(self.repo.engine, approval_grants)
                    .values(
                        tenant_id=scope.tenant_id,
                        space_id=scope.space_id,
                        user_id=scope.user_id,
                        tool_reference=effect["request"]["tool_reference"],
                    )
                    .on_conflict_do_nothing()
                )
            blocks = [{**block, "status": "answered", "answer": answer} for block in card["blocks"]]
            conn.execute(
                approval_requests.update()
                .where(approval_requests.c.effect_id == card["effect_id"])
                .values(answer=answer, blocks=blocks)
            )
            queued = (
                conn.execute(
                    runs.update()
                    .where(runs.c.run_id == run_id)
                    .values(
                        status="queued",
                        approval_resumes=runs.c.approval_resumes + 1,
                        event_seq=runs.c.event_seq + 1,
                        updated_at=self.repo.clock(),
                    )
                    .returning(runs)
                )
                .mappings()
                .one()
            )
            if row["thread_id"]:
                append(
                    conn,
                    queued,
                    "thread.message.updated",
                    {"messageId": message_id, "role": "bot", "blocks": blocks},
                )
            _event(
                conn,
                queued,
                "approval_answered",
                {"effect_id": card["effect_id"], "answer": answer},
            )
            return {"ok": True}

    def decide(self, scope, run_id, effect_id, *, decision, request_hash):
        # The REST review endpoint must resume the same workflow as native threads.answer.
        with self.repo.engine.connect() as conn:
            self.effects._owner(conn, scope, run_id)
            effect = self.effects._find(conn, run_id, effect_id)
            card = (
                conn.execute(
                    sa.select(approval_requests).where(approval_requests.c.effect_id == effect_id)
                )
                .mappings()
                .first()
            )
        if effect["request_hash"] != request_hash:
            raise RunConflict("Approval digest does not match")
        if card:
            self.answer(
                scope, run_id, card["message_id"], "allow" if decision == "approve" else "deny"
            )
            with self.repo.engine.connect() as conn:
                self.effects._owner(conn, scope, run_id)
                return self.effects._find(conn, run_id, effect_id)
        return self.effects.decide(
            scope, run_id, effect_id, decision=decision, request_hash=request_hash
        )
