"""Versioned assets and private conversations with explicit space sharing."""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunConflict, RunNotFound, Scope

from agentforge_storage.authorization import Authorization
from agentforge_storage.schema import agent_versions, agents, messages, runs, threads


def scoped(table, scope: Scope):
    return sa.and_(*(table.c[key] == value for key, value in asdict(scope).items()))


def agent_row(connection, scope, agent_id, version=None, *, authorization=None, owner_only=False):
    policy = authorization or Authorization()
    selected = version if version is not None else agents.c.version
    row = (
        connection.execute(
            sa.select(
                agents.c.agent_id,
                agents.c.computer_mode,
                agents.c.tenant_id,
                agents.c.space_id,
                agents.c.user_id,
                agent_versions.c.name,
                agent_versions.c.version,
                agent_versions.c.execution_config,
                agent_versions.c.created_at,
            )
            .join(
                agent_versions,
                sa.and_(
                    agent_versions.c.agent_id == agents.c.agent_id,
                    agent_versions.c.version == selected,
                ),
            )
            .where(agents.c.agent_id == agent_id)
        )
        .mappings()
        .first()
    )
    policy.check_row(connection, scope, "agent", row, owner_only=owner_only)
    if row["user_id"] != scope.user_id:
        policy.require_dependencies(
            connection, scope, ExecutionSnapshot.model_validate(row["execution_config"])
        )
    return {
        key: value for key, value in row.items() if key not in {"tenant_id", "space_id", "user_id"}
    }


def thread_row(connection, scope, thread_id, *, authorization=None, owner_only=False):
    policy = authorization or Authorization()
    row = (
        connection.execute(sa.select(threads).where(threads.c.thread_id == thread_id))
        .mappings()
        .first()
    )
    policy.check_row(connection, scope, "thread", row, owner_only=owner_only)
    return dict(row)


class ConversationRepository:
    def __init__(self, engine, clock, *, auth_mode="development"):
        self.engine = engine
        self.clock = clock
        self.authorization = Authorization(auth_mode)

    def create_agent(self, scope: Scope, name: str, config: ExecutionSnapshot, *, _connection=None):
        now = self.clock()
        agent_id = str(uuid4())
        with (
            nullcontext(_connection) if _connection is not None else self.engine.begin()
        ) as connection:
            self.authorization.require(connection, scope, write=True)
            self.authorization.require_dependencies(connection, scope, config)
            connection.execute(
                agents.insert().values(
                    agent_id=agent_id,
                    **asdict(scope),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                agent_versions.insert().values(
                    agent_id=agent_id,
                    version=1,
                    name=name,
                    execution_config=config.model_dump(mode="json"),
                    created_at=now,
                )
            )
            return agent_row(connection, scope, agent_id, authorization=self.authorization)

    def update_agent(self, scope, agent_id, expected_version, name, config, *, _connection=None):
        with (
            nullcontext(_connection) if _connection is not None else self.engine.begin()
        ) as connection:
            self.authorization.require(connection, scope, write=True)
            agent_row(
                connection, scope, agent_id, authorization=self.authorization, owner_only=True
            )
            self.authorization.require_dependencies(connection, scope, config)
            changed = connection.execute(
                agents.update()
                .where(
                    agents.c.agent_id == agent_id,
                    scoped(agents, scope),
                    agents.c.version == expected_version,
                )
                .values(version=expected_version + 1, updated_at=self.clock())
                .returning(agents.c.agent_id)
            ).scalar_one_or_none()
            if changed is None:
                agent_row(
                    connection, scope, agent_id, authorization=self.authorization, owner_only=True
                )
                raise RunConflict("Agent version changed")
            connection.execute(
                agent_versions.insert().values(
                    agent_id=agent_id,
                    version=expected_version + 1,
                    name=name,
                    execution_config=config.model_dump(mode="json"),
                    created_at=self.clock(),
                )
            )
            return agent_row(connection, scope, agent_id, authorization=self.authorization)

    def get_agent(self, scope, agent_id, version=None):
        with self.engine.connect() as connection:
            return agent_row(connection, scope, agent_id, version, authorization=self.authorization)

    def list_agents(self, scope, offset=0, limit=100):
        with self.engine.connect() as connection:
            self.authorization.require(connection, scope)
            query = (
                sa.select(agents.c.agent_id)
                .where(self.authorization.visible(agents, scope, "agent", "agent_id"))
                .order_by(agents.c.created_at, agents.c.agent_id)
            )
            # Dependency permissions can hide an otherwise shared Agent. Apply the
            # public offset after filtering so hidden rows do not truncate pages.
            scanned = visible = 0
            result = []
            while len(result) < limit:
                ids = connection.execute(query.offset(scanned).limit(100)).scalars().all()
                if not ids:
                    break
                scanned += len(ids)
                for agent_id in ids:
                    try:
                        item = agent_row(
                            connection, scope, agent_id, authorization=self.authorization
                        )
                    except RunNotFound:
                        continue
                    if visible >= offset:
                        result.append(item)
                    visible += 1
                    if len(result) == limit:
                        return result
            return result

    def create_thread(self, scope, agent_id, title, *, _connection=None, _thread_id=None):
        with (
            nullcontext(_connection) if _connection is not None else self.engine.begin()
        ) as connection:
            self.authorization.require(connection, scope, write=True)
            agent = agent_row(connection, scope, agent_id, authorization=self.authorization)
            self.authorization.require_dependencies(
                connection, scope, ExecutionSnapshot.model_validate(agent["execution_config"])
            )
            thread_id = _thread_id or str(uuid4())
            now = self.clock()
            connection.execute(
                threads.insert().values(
                    thread_id=thread_id,
                    **asdict(scope),
                    agent_id=agent_id,
                    agent_version=agent["version"],
                    title=title,
                    message_seq=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return thread_row(connection, scope, thread_id, authorization=self.authorization)

    def get_thread(self, scope, thread_id):
        with self.engine.connect() as connection:
            return thread_row(connection, scope, thread_id, authorization=self.authorization)

    def list_threads(self, scope, offset=0, limit=100):
        with self.engine.connect() as connection:
            self.authorization.require(connection, scope)
            return (
                connection.execute(
                    sa.select(threads)
                    .where(self.authorization.visible(threads, scope, "thread", "thread_id"))
                    .order_by(threads.c.created_at, threads.c.thread_id)
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
                .all()
            )

    def list_messages(self, scope, thread_id, after=0, limit=100):
        with self.engine.connect() as connection:
            thread_row(connection, scope, thread_id, authorization=self.authorization)
            return (
                connection.execute(
                    sa.select(messages, runs.c.status.label("run_status"))
                    .join(threads, messages.c.thread_id == threads.c.thread_id)
                    .join(runs, messages.c.run_id == runs.c.run_id)
                    .where(
                        self.authorization.visible(threads, scope, "thread", "thread_id"),
                        messages.c.thread_id == thread_id,
                        messages.c.sequence > after,
                    )
                    .order_by(messages.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
