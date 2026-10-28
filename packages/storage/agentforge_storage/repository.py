import re
import time
from collections.abc import Callable
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.models import (
    Claim,
    Run,
    RunConflict,
    RunEvent,
    RunNotFound,
    RunStatus,
    Scope,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentforge_storage.schema import events, runs


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


class RunRepository:
    """Durable database-backed queue for the offline, side-effect-free example."""

    def __init__(self, engine: sa.Engine, clock: Callable[[], float] = time.time):
        self.engine = engine
        self.clock = clock

    def create(self, scope: Scope, prompt: str, idempotency_key: str | None = None) -> Run:
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise ValueError("Invalid idempotency key")
        now = self.clock()
        insert = postgres_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        statement = insert(runs).values(
            run_id=str(uuid4()),
            tenant_id=scope.tenant_id,
            space_id=scope.space_id,
            user_id=scope.user_id,
            prompt=prompt,
            idempotency_key=idempotency_key,
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
        with self.engine.begin() as connection:
            row = connection.execute(statement.returning(runs)).mappings().first()
            if row is None:
                row = (
                    connection.execute(
                        sa.select(runs).where(
                            _scope_filter(scope), runs.c.idempotency_key == idempotency_key
                        )
                    )
                    .mappings()
                    .one()
                )
                if row["prompt"] != prompt:
                    raise RunConflict("Idempotency key was already used for another task")
                return _run(row)
            _event(connection, row, "queued", {})
            return _run(row)

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
            _event(connection, row, "tool_started", {"name": name})
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

    def finish(self, claim: Claim, *, result: str | None = None, failed: bool = False) -> bool:
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
                        error_code="execution_failed" if failed else None,
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
            _event(connection, row, status.value, {})
            return True
