import re
import time
from collections.abc import Callable
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.execution import ContextMessage, ExecutionSnapshot
from agentforge_core.models import (
    Claim,
    Run,
    RunConflict,
    RunErrorCode,
    RunEvent,
    RunNotFound,
    RunStatus,
    Scope,
)
from agentforge_core.tools import ToolInvocation, ToolTicket
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentforge_storage.conversations import agent_row, scoped, thread_row
from agentforge_storage.schema import attempts, events, messages, runs, threads, tool_calls


def _scope_filter(scope: Scope):
    return sa.and_(
        runs.c.tenant_id == scope.tenant_id,
        runs.c.space_id == scope.space_id,
        runs.c.user_id == scope.user_id,
    )


def _run(row) -> Run:
    return Run(
        run_id=row["run_id"],
        scope=Scope(row["tenant_id"], row["space_id"], row["user_id"]),
        prompt=row["prompt"],
        status=RunStatus(row["status"]),
        attempt=row["attempt"],
        result=row["result"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        thread_id=row["thread_id"],
        agent_id=row["agent_id"],
        agent_version=row["agent_version"],
        execution_config=(
            ExecutionSnapshot.model_validate(row["execution_config"])
            if row["execution_config"] is not None
            else None
        ),
        context=tuple(ContextMessage.model_validate(item) for item in (row["context"] or [])),
    )


def _event(connection, row, event_type: str, data: dict) -> None:
    # Called in the same transaction as the state change; never publish before commit.
    connection.execute(
        events.insert().values(
            run_id=row["run_id"],
            sequence=row["event_seq"],
            event_type=event_type,
            data=data,
            created_at=row["updated_at"],
        )
    )


class _ExistingRun(Exception):
    def __init__(self, run):
        self.run = run


def _complete_conversation(connection, row, now):
    if row["thread_id"] is None:
        return
    success = row["status"] == RunStatus.SUCCEEDED.value
    values = {"active_run_id": None, "updated_at": now}
    if success:
        values["message_seq"] = threads.c.message_seq + 1
    thread = (
        connection.execute(
            threads.update()
            .where(
                threads.c.thread_id == row["thread_id"],
                threads.c.active_run_id == row["run_id"],
            )
            .values(**values)
            .returning(threads)
        )
        .mappings()
        .first()
    )
    if thread is None:
        raise RunConflict("Conversation reservation is missing")
    if success:
        connection.execute(
            messages.insert().values(
                message_id=str(uuid4()),
                thread_id=row["thread_id"],
                run_id=row["run_id"],
                sequence=thread["message_seq"],
                role="assistant",
                content=row["result"] or "",
                created_at=now,
            )
        )


def _abandon_tool_calls(connection, row, reason):
    connection.execute(
        tool_calls.update()
        .where(
            tool_calls.c.run_id == row["run_id"],
            tool_calls.c.status == "running",
        )
        .values(status="abandoned", error_code=reason, finished_at=row["updated_at"])
    )


class RunRepository:
    """Durable database-backed queue for the offline, side-effect-free example."""

    def __init__(self, engine: sa.Engine, clock: Callable[[], float] = time.time):
        self.engine = engine
        self.clock = clock

    def create(
        self,
        scope: Scope,
        prompt: str,
        idempotency_key: str | None = None,
        *,
        thread_id: str | None = None,
        execution_config: ExecutionSnapshot | None = None,
    ) -> Run:
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise ValueError("Invalid idempotency key")
        if not prompt.strip() or len(prompt) > 10000:
            raise ValueError("Invalid prompt")
        now = self.clock()
        run_id = str(uuid4())
        insert = postgres_insert if self.engine.dialect.name == "postgresql" else sqlite_insert

        def existing(connection):
            if idempotency_key is None:
                return None
            row = (
                connection.execute(
                    sa.select(runs).where(
                        _scope_filter(scope),
                        runs.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if row is not None and (row["prompt"] != prompt or row["thread_id"] != thread_id):
                raise RunConflict("Idempotency key was already used for another request")
            return row

        try:
            with self.engine.begin() as connection:
                previous = existing(connection)
                if previous is not None:
                    return _run(previous)
                config = execution_config or ExecutionSnapshot()
                context = []
                thread = None
                if thread_id is not None:
                    thread = (
                        connection.execute(
                            threads.update()
                            .where(
                                threads.c.thread_id == thread_id,
                                scoped(threads, scope),
                                threads.c.active_run_id.is_(None),
                            )
                            .values(
                                active_run_id=run_id,
                                message_seq=threads.c.message_seq + 1,
                                updated_at=now,
                            )
                            .returning(threads)
                        )
                        .mappings()
                        .first()
                    )
                    if thread is None:
                        thread_row(connection, scope, thread_id)
                        previous = existing(connection)
                        if previous is not None:
                            return _run(previous)
                        raise RunConflict("The conversation already has an active run")
                    agent = agent_row(
                        connection, scope, thread["agent_id"], thread["agent_version"]
                    )
                    config = ExecutionSnapshot.model_validate(agent["execution_config"])
                    # Recent complete turns only: cancelled/failed prompts never masquerade
                    # as successful conversation context. Stored once, reused by all attempts.
                    history = (
                        connection.execute(
                            sa.select(messages.c.role, messages.c.content)
                            .join(runs, messages.c.run_id == runs.c.run_id)
                            .where(
                                messages.c.thread_id == thread_id,
                                _scope_filter(scope),
                                runs.c.status == RunStatus.SUCCEEDED.value,
                            )
                            .order_by(messages.c.sequence.desc())
                            .limit(40)
                        )
                        .mappings()
                        .all()
                    )
                    context = [dict(item) for item in reversed(history)]
                statement = insert(runs).values(
                    run_id=run_id,
                    tenant_id=scope.tenant_id,
                    space_id=scope.space_id,
                    user_id=scope.user_id,
                    prompt=prompt,
                    idempotency_key=idempotency_key,
                    thread_id=thread_id,
                    agent_id=thread["agent_id"] if thread else None,
                    agent_version=thread["agent_version"] if thread else None,
                    execution_config=config.model_dump(mode="json"),
                    context=context,
                    status=RunStatus.QUEUED.value,
                    attempt=0,
                    event_seq=1,
                    created_at=now,
                    updated_at=now,
                )
                if idempotency_key is not None:
                    statement = statement.on_conflict_do_nothing(
                        index_elements=["tenant_id", "space_id", "user_id", "idempotency_key"]
                    )
                row = connection.execute(statement.returning(runs)).mappings().first()
                if row is None:
                    # Roll back any conversation reservation before returning the winner.
                    raise _ExistingRun(_run(existing(connection)))
                if thread is not None:
                    connection.execute(
                        messages.insert().values(
                            message_id=str(uuid4()),
                            thread_id=thread_id,
                            run_id=run_id,
                            sequence=thread["message_seq"],
                            role="user",
                            content=prompt,
                            created_at=now,
                        )
                    )
                _event(connection, row, "queued", {})
                return _run(row)
        except _ExistingRun as duplicate:
            return duplicate.run

    def get(self, scope: Scope, run_id: str) -> Run:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(runs).where(runs.c.run_id == run_id, _scope_filter(scope))
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RunNotFound()
        return _run(row)

    def list_events(
        self, scope: Scope, run_id: str, after: int = 0, limit: int = 100
    ) -> list[RunEvent]:
        self.get(scope, run_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(events)
                    .join(runs, events.c.run_id == runs.c.run_id)
                    .where(
                        _scope_filter(scope), events.c.run_id == run_id, events.c.sequence > after
                    )
                    .order_by(events.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [RunEvent(**row) for row in rows]

    def cancel(self, scope: Scope, run_id: str) -> Run:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    runs.update()
                    .where(
                        runs.c.run_id == run_id,
                        _scope_filter(scope),
                        runs.c.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]),
                    )
                    .values(
                        status=RunStatus.CANCELLED.value,
                        attempt_id=None,
                        lease_until=None,
                        event_seq=runs.c.event_seq + 1,
                        updated_at=self.clock(),
                    )
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            if row is not None:
                connection.execute(
                    attempts.update()
                    .where(
                        attempts.c.run_id == run_id,
                        attempts.c.status == "running",
                    )
                    .values(status="cancelled", finished_at=row["updated_at"])
                )
                _abandon_tool_calls(connection, row, "run_cancelled")
                _complete_conversation(connection, row, row["updated_at"])
                _event(connection, row, "cancelled", {})
                return _run(row)
        current = self.get(scope, run_id)
        if current.status == RunStatus.CANCELLED:
            return current
        raise RunConflict("A finished task cannot be cancelled")

    def claim_next(self, lease_seconds: int = 60, max_attempts: int = 3) -> Claim | None:
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("Lease duration and max attempts must be positive")
        now = self.clock()
        eligible = sa.or_(
            runs.c.status == RunStatus.QUEUED.value,
            sa.and_(runs.c.status == RunStatus.RUNNING.value, runs.c.lease_until <= now),
        )
        with self.engine.connect() as connection:
            candidates = (
                connection.execute(
                    sa.select(runs.c.run_id, runs.c.attempt)
                    .where(eligible)
                    .order_by(runs.c.created_at, runs.c.run_id)
                    .limit(16)
                )
                .mappings()
                .all()
            )
        for candidate in candidates:
            attempt_id = str(uuid4())
            exhausted = candidate["attempt"] >= max_attempts
            with self.engine.begin() as connection:
                # Compare-and-swap prevents two Workers from owning the same attempt.
                row = (
                    connection.execute(
                        runs.update()
                        .where(
                            runs.c.run_id == candidate["run_id"],
                            runs.c.attempt == candidate["attempt"],
                            eligible,
                        )
                        .values(
                            status=RunStatus.FAILED.value if exhausted else RunStatus.RUNNING.value,
                            attempt=candidate["attempt"] if exhausted else candidate["attempt"] + 1,
                            attempt_id=None if exhausted else attempt_id,
                            lease_until=None if exhausted else now + lease_seconds,
                            error_code="retry_limit_exceeded" if exhausted else None,
                            updated_at=now,
                            event_seq=runs.c.event_seq + 1,
                        )
                        .returning(runs)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                connection.execute(
                    attempts.update()
                    .where(
                        attempts.c.run_id == row["run_id"],
                        attempts.c.status == "running",
                    )
                    .values(status="expired", error_code="lease_expired", finished_at=now)
                )
                _abandon_tool_calls(connection, row, "lease_expired")
                if exhausted:
                    _complete_conversation(connection, row, now)
                else:
                    connection.execute(
                        attempts.insert().values(
                            attempt_id=attempt_id,
                            run_id=row["run_id"],
                            number=row["attempt"],
                            status="running",
                            started_at=now,
                        )
                    )
                _event(
                    connection,
                    row,
                    "failed" if exhausted else "running",
                    {"attempt": row["attempt"]},
                )
                if not exhausted:
                    return Claim(_run(row), attempt_id)
        return None

    def _owned(self, claim: Claim, now: float):
        return sa.and_(
            runs.c.run_id == claim.run.run_id,
            _scope_filter(claim.run.scope),
            runs.c.status == RunStatus.RUNNING.value,
            runs.c.attempt_id == claim.attempt_id,
            runs.c.lease_until > now,
        )

    def record_tool(self, claim: Claim, name: str) -> bool:
        return self._record_attempt_event(claim, "tool_started", {"name": name})

    def owns_claim(self, claim: Claim) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(runs.c.run_id).where(self._owned(claim, self.clock()))
                ).scalar_one_or_none()
                is not None
            )

    def record_model(self, claim: Claim, *, step: int, model: str, usage: dict | None) -> bool:
        return self._record_attempt_event(
            claim,
            "model_completed",
            {"attempt": claim.run.attempt, "step": step, "model": model, "usage": usage},
        )

    def _record_attempt_event(self, claim: Claim, event_type: str, data: dict) -> bool:
        now = self.clock()
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    runs.update()
                    .where(self._owned(claim, now))
                    .values(event_seq=runs.c.event_seq + 1, updated_at=now)
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            _event(connection, row, event_type, data)
            return True

    def renew_lease(self, claim: Claim, lease_seconds: int = 60) -> bool:
        if lease_seconds < 1:
            raise ValueError("Lease duration must be positive")
        now = self.clock()
        with self.engine.begin() as connection:
            run_id = connection.execute(
                runs.update()
                .where(self._owned(claim, now))
                .values(lease_until=now + lease_seconds)
                .returning(runs.c.run_id)
            ).scalar_one_or_none()
            return run_id is not None

    def finish(
        self,
        claim: Claim,
        *,
        result: str | None = None,
        failed: bool = False,
        error_code: RunErrorCode = RunErrorCode.EXECUTION_FAILED,
    ) -> bool:
        if not isinstance(error_code, RunErrorCode):
            raise ValueError("Expected a known run error code")
        now = self.clock()
        status = RunStatus.FAILED if failed else RunStatus.SUCCEEDED
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    runs.update()
                    .where(self._owned(claim, now))
                    .values(
                        status=status.value,
                        result=None if failed else result,
                        error_code=error_code.value if failed else None,
                        lease_until=None,
                        attempt_id=None,
                        updated_at=now,
                        event_seq=runs.c.event_seq + 1,
                    )
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            connection.execute(
                attempts.update()
                .where(
                    attempts.c.attempt_id == claim.attempt_id,
                    attempts.c.status == "running",
                )
                .values(
                    status=status.value,
                    error_code=error_code.value if failed else None,
                    finished_at=now,
                )
            )
            _abandon_tool_calls(connection, row, "run_finished")
            _complete_conversation(connection, row, now)
            _event(connection, row, status.value, {})
            return True

    def list_attempts(self, scope: Scope, run_id: str):
        self.get(scope, run_id)
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(attempts)
                    .join(runs, attempts.c.run_id == runs.c.run_id)
                    .where(_scope_filter(scope), attempts.c.run_id == run_id)
                    .order_by(attempts.c.number)
                )
                .mappings()
                .all()
            )

    def start_tool(self, claim: Claim, invocation: ToolInvocation) -> ToolTicket | None:
        now = self.clock()
        with self.engine.begin() as connection:
            # Serialize duplicate tool starts with completion/cancellation through the Run row.
            row = (
                connection.execute(
                    runs.update()
                    .where(self._owned(claim, now))
                    .values(updated_at=now)
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            previous = (
                connection.execute(
                    sa.select(tool_calls).where(
                        tool_calls.c.attempt_id == claim.attempt_id,
                        tool_calls.c.call_id == invocation.call_id,
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if (
                    previous["name"] != invocation.name
                    or previous["version"] != invocation.version
                    or previous["arguments_hash"] != invocation.arguments_hash
                    or previous["status"] != "succeeded"
                ):
                    raise RunConflict("Tool invocation is already claimed or changed")
                return ToolTicket(
                    previous["tool_call_id"], cached=True, content=previous["content"]
                )
            row = (
                connection.execute(
                    runs.update()
                    .where(runs.c.run_id == claim.run.run_id)
                    .values(event_seq=runs.c.event_seq + 1)
                    .returning(runs)
                )
                .mappings()
                .one()
            )
            tool_call_id = str(uuid4())
            connection.execute(
                tool_calls.insert().values(
                    tool_call_id=tool_call_id,
                    run_id=row["run_id"],
                    attempt_id=claim.attempt_id,
                    sequence=row["event_seq"],
                    call_id=invocation.call_id,
                    name=invocation.name,
                    version=invocation.version,
                    arguments=invocation.arguments,
                    arguments_hash=invocation.arguments_hash,
                    status="running",
                    started_at=now,
                )
            )
            _event(
                connection,
                row,
                "tool_started",
                {
                    "tool_call_id": tool_call_id,
                    "call_id": invocation.call_id,
                    "name": invocation.name,
                    "version": invocation.version,
                    "attempt": row["attempt"],
                },
            )
            return ToolTicket(tool_call_id)

    def complete_tool(
        self,
        claim: Claim,
        ticket: ToolTicket,
        *,
        output: dict | None = None,
        content: str | None = None,
        error_code: RunErrorCode | None = None,
    ) -> bool:
        if error_code is not None and not isinstance(error_code, RunErrorCode):
            raise ValueError("Expected a public tool failure code")
        now = self.clock()
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    runs.update()
                    .where(self._owned(claim, now))
                    .values(updated_at=now)
                    .returning(runs)
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            call = (
                connection.execute(
                    tool_calls.update()
                    .where(
                        tool_calls.c.tool_call_id == ticket.tool_call_id,
                        tool_calls.c.run_id == claim.run.run_id,
                        tool_calls.c.attempt_id == claim.attempt_id,
                        tool_calls.c.status == "running",
                    )
                    .values(
                        status="failed" if error_code else "succeeded",
                        output=output if error_code is None else None,
                        content=content if error_code is None else None,
                        error_code=error_code.value if error_code else None,
                        finished_at=now,
                    )
                    .returning(tool_calls)
                )
                .mappings()
                .first()
            )
            if call is None:
                return False
            row = (
                connection.execute(
                    runs.update()
                    .where(runs.c.run_id == claim.run.run_id)
                    .values(event_seq=runs.c.event_seq + 1)
                    .returning(runs)
                )
                .mappings()
                .one()
            )
            _event(
                connection,
                row,
                "tool_failed" if error_code else "tool_completed",
                {
                    "tool_call_id": ticket.tool_call_id,
                    "name": call["name"],
                    "version": call["version"],
                    "attempt": row["attempt"],
                    "error_code": error_code.value if error_code else None,
                },
            )
            return True

    def list_tool_calls(self, scope: Scope, run_id: str, after: int = 0, limit: int = 100):
        self.get(scope, run_id)
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(tool_calls)
                    .join(runs, tool_calls.c.run_id == runs.c.run_id)
                    .where(
                        _scope_filter(scope),
                        tool_calls.c.run_id == run_id,
                        tool_calls.c.sequence > after,
                    )
                    .order_by(tool_calls.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
