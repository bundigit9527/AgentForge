from pathlib import Path

import sqlalchemy as sa
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import metadata
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext


def test_migration_matches_schema_and_supports_round_trip(engine):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def test_upgrade_preserves_existing_tasks_and_events(engine, scope, client):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")
        connection.execute(
            sa.text(
                "INSERT INTO runs (run_id, tenant_id, space_id, user_id, "
                "prompt, status, attempt, "
                "event_seq, created_at, updated_at) VALUES "
                "('legacy', :tenant, :space, :user, 'existing', 'queued', 0, 1, 1, 1)"
            ),
            {"tenant": scope.tenant_id, "space": scope.space_id, "user": scope.user_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events (run_id, sequence, event_type, data, created_at) "
                "VALUES ('legacy', 1, 'queued', '{}', 1)"
            )
        )
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    repository = RunRepository(engine)
    assert repository.get(scope, "legacy").prompt == "existing"
    assert len(repository.list_events(scope, "legacy")) == 1
    first = repository.create(scope, "new", "key")
    assert repository.create(scope, "new", "key").run_id == first.run_id


def test_legacy_runs_remain_readable_but_missing_configuration_is_not_guessed(engine, scope):
    from agentforge_worker.worker import Worker

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002")
        for run_id, status, result in (
            ("legacy-pending", "queued", None),
            ("legacy-done", "succeeded", "old result"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO runs (run_id, tenant_id, space_id, user_id, "
                    "prompt, status, attempt, "
                    "event_seq, created_at, updated_at, result) VALUES "
                    "(:id, :tenant, :space, :user, 'old prompt', :status, 0, 1, 1, 1, :result)"
                ),
                {
                    "id": run_id,
                    "tenant": scope.tenant_id,
                    "space": scope.space_id,
                    "user": scope.user_id,
                    "status": status,
                    "result": result,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO run_events (run_id, sequence, event_type, data, created_at) "
                    "VALUES (:id, 1, :status, '{}', 1)"
                ),
                {"id": run_id, "status": status},
            )
        command.upgrade(config, "head")
    repository = RunRepository(engine)
    assert repository.get(scope, "legacy-done").result == "old result"
    assert repository.get(scope, "legacy-pending").execution_config is None
    assert Worker(repository).run_once()
    assert repository.get(scope, "legacy-pending").error_code == "execution_config_missing"
    assert [event.event_type for event in repository.list_events(scope, "legacy-pending")] == [
        "queued",
        "running",
        "failed",
    ]
    assert not Worker(repository).run_once()


def test_tool_migration_preserves_conversation_run_and_config(engine, scope, client):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_storage.conversations import ConversationRepository
    from agentforge_worker.worker import Worker

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    repository = RunRepository(engine)
    conversations = ConversationRepository(engine, repository.clock)
    agent = conversations.create_agent(scope, "existing", ExecutionSnapshot(instructions="keep"))
    thread = conversations.create_thread(scope, agent["agent_id"], "existing conversation")
    run = repository.create(scope, "existing prompt", thread_id=thread["thread_id"])
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0003")
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    assert repository.get(scope, run.run_id).execution_config.instructions == "keep"
    assert len(conversations.list_messages(scope, thread["thread_id"])) == 1
    assert repository.list_tool_calls(scope, run.run_id) == []
    Worker(repository).run_once()
    assert len(conversations.list_messages(scope, thread["thread_id"])) == 2
    assert repository.list_tool_calls(scope, run.run_id)[0]["status"] == "succeeded"


def test_skill_migration_preserves_existing_run_and_tool_history(engine, scope, client):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_worker.worker import Worker

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    repository = RunRepository(engine)
    run = repository.create(scope, "before skills")
    Worker(repository).run_once()
    existing_calls = repository.list_tool_calls(scope, run.run_id)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004")
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    assert repository.get(scope, run.run_id).status == "succeeded"
    assert repository.list_tool_calls(scope, run.run_id) == existing_calls
    legacy = ExecutionSnapshot.model_validate({"version": 1, "harness_version": 1, "tools": []})
    assert legacy.skills == ()


def test_identity_upgrade_preserves_legacy_resources_without_creating_accounts(
    engine, scope, client
):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_storage.identity import IdentityRepository
    from agentforge_storage.schema import identity_users, runs
    from agentforge_storage.skills import SkillRepository
    from agentforge_worker.worker import Worker

    from tests.test_skills import recipe

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    repository = RunRepository(engine)
    skill_store = SkillRepository(engine, repository.clock)
    manifest = recipe()
    skill = skill_store.install(scope, "hub:" + "a" * 64, manifest, manifest.digest())
    run = repository.create(
        scope, "before identity", execution_config=ExecutionSnapshot(skills=(skill,))
    )
    # Historical JSON did not contain the authorization mode.
    historical = run.execution_config.model_dump(mode="json", exclude={"authorization_mode"})
    with engine.begin() as connection:
        connection.execute(
            runs.update().where(runs.c.run_id == run.run_id).values(execution_config=historical)
        )
    Worker(repository).run_once()
    calls = repository.list_tool_calls(scope, run.run_id)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0005")
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(identity_users)).scalar_one()
            == 0
        )
    assert client.get("/health/ready").status_code == 200
    assert repository.get(scope, run.run_id).status == "succeeded"
    assert repository.get(scope, run.run_id).execution_config.authorization_mode == "development"
    assert repository.list_tool_calls(scope, run.run_id) == calls
    assert skill_store.list(scope, 0, 10) == [skill]
    IdentityRepository(engine).bootstrap(
        scope, tenant_name="Existing", space_name="Existing", display_name="Owner"
    )
    secured = RunRepository(engine, auth_mode="database")
    assert secured.get(scope, run.run_id).result == repository.get(scope, run.run_id).result


def test_rpc_migration_backfills_messages_and_preserves_execution(engine, scope, client):
    from agentforge_core.execution import ExecutionSnapshot
    from agentforge_storage.conversations import ConversationRepository
    from agentforge_storage.rakazo import RakazoRepository
    from agentforge_worker.worker import Worker

    repo = RunRepository(engine)
    service = RakazoRepository(repo)
    agent = ConversationRepository(engine, repo.clock).create_agent(
        scope, "Existing", ExecutionSnapshot()
    )
    tid = service.ensure_thread(scope, agent["agent_id"])
    first = repo.create(scope, "legacy message", thread_id=tid)
    Worker(repo).run_once()
    original = service.history(scope, agent["agent_id"])["messages"]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0006")
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    historical = service.history(scope, agent["agent_id"])["messages"]
    # Downgrading before the RPC event table deliberately removes its UI projection.
    # The original upgrade backfills saved conversation text, not later tool-activity blocks.
    assert [(m["id"], [b for b in m["blocks"] if b["kind"] == "text"]) for m in original] == [
        (m["id"], m["blocks"]) for m in historical
    ]
    assert repo.get(scope, first.run_id).status == "succeeded"
    second = service.send(scope, {"botId": agent["agent_id"], "text": "after upgrade"})
    assert second["seq"] > historical[-1]["seq"]
    Worker(repo).run_once()
    assert len(service.snapshot(scope, agent["agent_id"])["messages"]) == 4
