from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from agentforge_core.models import RunConflict, RunStatus
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker

from tests.postgresql_support import isolated_postgresql_database

pytestmark = pytest.mark.postgresql


def test_connections_use_only_the_isolated_schema(engine):
    with engine.connect() as first, engine.connect() as second:
        schemas = first.execute(sa.text("SELECT current_schemas(false)")).scalar_one()
        assert len(schemas) == 1
        assert schemas[0].startswith("af_test_")
        assert second.execute(sa.text("SELECT current_schema()")).scalar_one() == schemas[0]


def test_task_and_events_survive_engine_recreation(client, headers, database_url, scope):
    run_id = client.post(
        "/api/v1/runs", json={"prompt": "persistent task"}, headers=headers
    ).json()["run_id"]
    worker_engine = create_database(database_url)
    try:
        assert Worker(RunRepository(worker_engine)).run_once()
    finally:
        worker_engine.dispose()
    reader_engine = create_database(database_url)
    try:
        reader = RunRepository(reader_engine)
        assert reader.get(scope, run_id).status == RunStatus.SUCCEEDED
        assert [event.event_type for event in reader.list_events(scope, run_id)] == [
            "queued",
            "running",
            "tool_started",
            "tool_completed",
            "succeeded",
        ]
    finally:
        reader_engine.dispose()
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "succeeded"


def test_concurrent_workers_drain_queue_without_duplicate_execution(
    repository, scope, database_url
):
    expected = {repository.create(scope, f"task {index}").run_id for index in range(24)}
    start = Barrier(4, timeout=10)

    def consume(_):
        engine = create_database(database_url)
        try:
            worker_repository = RunRepository(engine)
            claimed = []
            start.wait()
            while claim := worker_repository.claim_next():
                claimed.append(claim.run.run_id)
                assert worker_repository.finish(claim, result="completed")
            return claimed
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(consume, index) for index in range(4)]
        processed = [run_id for future in futures for run_id in future.result(timeout=30)]
    assert len(processed) == len(expected)
    assert set(processed) == expected
    for run_id in expected:
        assert repository.get(scope, run_id).attempt == 1
        assert [event.sequence for event in repository.list_events(scope, run_id)] == [1, 2, 3]


def test_cancel_racing_completion_has_one_terminal_event(repository, scope):
    run = repository.create(scope, "race")
    claim = repository.claim_next()
    start = Barrier(2, timeout=10)

    def cancel():
        start.wait()
        try:
            repository.cancel(scope, run.run_id)
            return True
        except RunConflict:
            return False

    def finish():
        start.wait()
        return repository.finish(claim, result="finished")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(cancel)
        finished = pool.submit(finish)
        assert cancelled.result(timeout=20) != finished.result(timeout=20)
    current = repository.get(scope, run.run_id)
    events = repository.list_events(scope, run.run_id)
    assert current.status in {RunStatus.CANCELLED, RunStatus.SUCCEEDED}
    assert [event.event_type for event in events] == ["queued", "running", current.status.value]
    assert current.result == ("finished" if current.status == RunStatus.SUCCEEDED else None)


def test_schema_cleanup_after_failure_preserves_other_test_data(
    postgresql_url, engine, repository, scope
):
    run = repository.create(scope, "keep this schema")
    with pytest.raises(RuntimeError, match="simulated test failure"):
        with isolated_postgresql_database(postgresql_url) as database:
            nested_engine = create_database(database.url)
            try:
                with nested_engine.begin() as connection:
                    connection.execute(sa.text("CREATE TABLE cleanup_probe (id integer)"))
            finally:
                nested_engine.dispose()
            schema = database.schema
            raise RuntimeError("simulated test failure")
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM pg_namespace WHERE nspname = :schema"),
                {"schema": schema},
            ).scalar_one()
            == 0
        )
    assert repository.get(scope, run.run_id).prompt == "keep this schema"
