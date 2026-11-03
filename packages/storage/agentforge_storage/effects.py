"""Approval ledger. Dispatch is reserved before I/O and never automatically replayed."""

import json
import re
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.effects import EffectRequest, bounded_json
from agentforge_core.models import TERMINAL_STATUSES, RunConflict, RunNotFound
from agentforge_core.reports import CsvSummary, exports_csv

from agentforge_storage.artifacts import save_csv
from agentforge_storage.repository import _event, write_connection
from agentforge_storage.schema import external_effects as effects
from agentforge_storage.schema import runs


class ExternalEffectRepository:
    def __init__(self, repository):
        self.repository = repository

    def _lock_claim(self, connection, claim):
        repo = self.repository
        row = (
            connection.execute(
                runs.update()
                .where(repo._owned(claim, repo.clock()))
                .values(updated_at=repo.clock())
                .returning(runs)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RunConflict("Execution no longer owns this run")
        return row

    def _owner(self, connection, scope, run_id, *, write=False):
        repo = self.repository
        repo.authorization.require(connection, scope, write=write)
        if write:
            # Serialize decisions with cancellation and worker actions (Run first).
            from agentforge_storage.authorization import owned

            row = (
                connection.execute(
                    runs.update()
                    .where(runs.c.run_id == run_id, owned(runs, scope))
                    .values(updated_at=repo.clock())
                    .returning(runs)
                )
                .mappings()
                .first()
            )
        else:
            row = (
                connection.execute(sa.select(runs).where(runs.c.run_id == run_id))
                .mappings()
                .first()
            )
        repo.authorization.check_row(connection, scope, "run", row, owner_only=True)
        if write and row["status"] in TERMINAL_STATUSES:
            raise RunConflict("Run has finished")
        return row

    def _find(self, connection, run_id, effect_id):
        row = (
            connection.execute(
                sa.select(effects).where(
                    effects.c.run_id == run_id, effects.c.effect_id == effect_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RunNotFound()
        return dict(row)

    def _event(self, connection, run, effect_id, status):
        row = (
            connection.execute(
                runs.update()
                .where(runs.c.run_id == run["run_id"])
                .values(event_seq=runs.c.event_seq + 1, updated_at=self.repository.clock())
                .returning(runs)
            )
            .mappings()
            .one()
        )
        _event(connection, row, "effect_updated", {"effect_id": effect_id, "status": status})

    def request(self, claim, operation_key: str, request: EffectRequest):
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", operation_key):
            raise ValueError("Invalid operation key")
        # Revalidate even a constructed or subsequently mutated Pydantic value.
        request = EffectRequest.model_validate_json(bounded_json(request.model_dump()))
        digest = request.digest()
        with self.repository.engine.begin() as connection:
            run = self._lock_claim(connection, claim)
            old = (
                connection.execute(
                    sa.select(effects).where(
                        effects.c.run_id == run["run_id"], effects.c.operation_key == operation_key
                    )
                )
                .mappings()
                .first()
            )
            if old is not None:
                if old["request_hash"] != digest:
                    raise RunConflict("Operation key already binds different arguments")
                return dict(old)
            effect_id = str(uuid4())
            connection.execute(
                effects.insert().values(
                    effect_id=effect_id,
                    run_id=run["run_id"],
                    operation_key=operation_key,
                    request=request.model_dump(),
                    request_hash=digest,
                    status="pending",
                    created_at=self.repository.clock(),
                    updated_at=self.repository.clock(),
                )
            )
            self._event(connection, run, effect_id, "pending")
            return self._find(connection, run["run_id"], effect_id)

    def list(self, scope, run_id, *, offset=0, limit=100):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid pagination")
        with self.repository.engine.connect() as connection:
            self._owner(connection, scope, run_id)
            return [
                dict(row)
                for row in connection.execute(
                    sa.select(effects)
                    .where(effects.c.run_id == run_id)
                    .order_by(effects.c.created_at, effects.c.effect_id)
                    .offset(offset)
                    .limit(limit)
                ).mappings()
            ]

    def decide(self, scope, run_id, effect_id, *, decision, request_hash, _connection=None):
        if decision not in {"approve", "reject"}:
            raise ValueError("Invalid decision")
        status = "approved" if decision == "approve" else "rejected"
        with write_connection(self.repository.engine, _connection) as connection:
            run = self._owner(connection, scope, run_id, write=True)
            effect = self._find(connection, run_id, effect_id)
            if effect["request_hash"] != request_hash:
                raise RunConflict("Review does not match the operation")
            if effect["status"] == status:
                return effect
            if effect["status"] != "pending":
                raise RunConflict("Operation has already been reviewed or dispatched")
            connection.execute(
                effects.update()
                .where(effects.c.effect_id == effect_id)
                .values(
                    status=status, reviewed_by=scope.user_id, updated_at=self.repository.clock()
                )
            )
            self._event(connection, run, effect_id, status)
            return self._find(connection, run_id, effect_id)

    def begin_dispatch(self, claim, effect_id):
        """Only the transaction winner may send; caller must still authorize the tool."""
        with self.repository.engine.begin() as connection:
            run = self._lock_claim(connection, claim)
            effect = self._find(connection, run["run_id"], effect_id)
            if effect["status"] != "approved":
                raise RunConflict("Operation is not approved or has already been dispatched")
            connection.execute(
                effects.update()
                .where(effects.c.effect_id == effect_id)
                .values(
                    status="dispatched",
                    dispatch_attempt_id=claim.attempt_id,
                    updated_at=self.repository.clock(),
                )
            )
            self._event(connection, run, effect_id, "dispatched")
            return self._find(connection, run["run_id"], effect_id)

    def complete(self, claim, effect_id, *, succeeded: bool, result: dict):
        if type(succeeded) is not bool or not isinstance(result, dict):
            raise ValueError("Invalid operation outcome")
        result = json.loads(bounded_json(result))
        status = "succeeded" if succeeded else "failed"
        with self.repository.engine.begin() as connection:
            run = self._lock_claim(connection, claim)
            effect = self._find(connection, run["run_id"], effect_id)
            if effect["dispatch_attempt_id"] != claim.attempt_id:
                raise RunConflict("Only the dispatching attempt may record a result")
            if effect["status"] == status and effect["result"] == result:
                return effect
            if effect["status"] != "dispatched":
                raise RunConflict("Operation result is already fixed")
            connection.execute(
                effects.update()
                .where(effects.c.effect_id == effect_id)
                .values(status=status, result=result, updated_at=self.repository.clock())
            )
            snapshot = claim.run.execution_config
            if (
                succeeded
                and snapshot
                and any(
                    exports_csv(tool) and tool.reference == effect["request"]["tool_reference"]
                    for tool in snapshot.mcp_tools
                )
            ):
                report = CsvSummary.model_validate(json.loads(result["content"])["structured"])
                save_csv(connection, run, effect_id, report.csv.encode("utf-8"))
            self._event(connection, run, effect_id, status)
            return self._find(connection, run["run_id"], effect_id)
