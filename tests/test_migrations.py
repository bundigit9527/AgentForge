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
                "INSERT INTO runs (run_id, tenant_id, space_id, user_id, prompt, status, attempt, "
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
