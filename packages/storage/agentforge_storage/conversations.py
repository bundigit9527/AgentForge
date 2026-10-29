"""Scoped Agent revisions and conversations; no enterprise identity provider yet."""

from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunConflict, RunNotFound, Scope

from agentforge_storage.schema import agent_versions, agents, messages, runs, threads


def scoped(table, scope: Scope):
    return sa.and_(*(table.c[key] == value for key, value in asdict(scope).items()))


def agent_row(connection, scope, agent_id, version=None):
    selected = version if version is not None else agents.c.version
    row = (
        connection.execute(
            sa.select(
                agents.c.agent_id,
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
            .where(agents.c.agent_id == agent_id, scoped(agents, scope))
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RunNotFound()
    return dict(row)


def thread_row(connection, scope, thread_id):
    row = (
        connection.execute(
            sa.select(threads).where(threads.c.thread_id == thread_id, scoped(threads, scope))
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RunNotFound()
    return dict(row)


class ConversationRepository:
    def __init__(self, engine, clock):
        self.engine = engine
        self.clock = clock

    def create_agent(self, scope: Scope, name: str, config: ExecutionSnapshot):
        now = self.clock()
        agent_id = str(uuid4())
        with self.engine.begin() as connection:
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
            return agent_row(connection, scope, agent_id)

    def update_agent(self, scope, agent_id, expected_version, name, config):
        with self.engine.begin() as connection:
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
                agent_row(connection, scope, agent_id)
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
            return agent_row(connection, scope, agent_id)

    def get_agent(self, scope, agent_id, version=None):
        with self.engine.connect() as connection:
            return agent_row(connection, scope, agent_id, version)

    def list_agents(self, scope, offset=0, limit=100):
        with self.engine.connect() as connection:
            ids = (
                connection.execute(
                    sa.select(agents.c.agent_id)
                    .where(scoped(agents, scope))
                    .order_by(agents.c.created_at, agents.c.agent_id)
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [agent_row(connection, scope, agent_id) for agent_id in ids]

    def create_thread(self, scope, agent_id, title):
        with self.engine.begin() as connection:
            agent = agent_row(connection, scope, agent_id)
            thread_id = str(uuid4())
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
            return thread_row(connection, scope, thread_id)

    def get_thread(self, scope, thread_id):
        with self.engine.connect() as connection:
            return thread_row(connection, scope, thread_id)

    def list_threads(self, scope, offset=0, limit=100):
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(threads)
                    .where(scoped(threads, scope))
                    .order_by(threads.c.created_at, threads.c.thread_id)
                    .offset(offset)
                    .limit(limit)
                )
                .mappings()
                .all()
            )

    def list_messages(self, scope, thread_id, after=0, limit=100):
        with self.engine.connect() as connection:
            thread_row(connection, scope, thread_id)
            return (
                connection.execute(
                    sa.select(messages, runs.c.status.label("run_status"))
                    .join(threads, messages.c.thread_id == threads.c.thread_id)
                    .join(runs, messages.c.run_id == runs.c.run_id)
                    .where(
                        scoped(threads, scope),
                        messages.c.thread_id == thread_id,
                        messages.c.sequence > after,
                    )
                    .order_by(messages.c.sequence)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
