from concurrent.futures import ThreadPoolExecutor

import pytest
from agentforge_core.models import RunConflict, Scope
from agentforge_worker.worker import Worker


def test_network_retry_returns_same_run_and_current_result(client, headers, repository):
    headers = {**headers, "Idempotency-Key": "report-request-1"}
    first = client.post("/api/v1/runs", json={"prompt": "report"}, headers=headers)
    Worker(repository).run_once()
    retry = client.post("/api/v1/runs", json={"prompt": "report"}, headers=headers)
    assert first.status_code == retry.status_code == 201
    assert first.json()["run_id"] == retry.json()["run_id"]
    assert retry.json()["status"] == "succeeded"
    assert not Worker(repository).run_once()
    conflict = client.post("/api/v1/runs", json={"prompt": "another report"}, headers=headers)
    assert conflict.status_code == 409


def test_concurrent_submission_uses_database_unique_boundary(repository, scope):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repository.create(scope, "same", "same-key"), range(4)))
    assert len({run.run_id for run in results}) == 1
    assert len(repository.list_events(scope, results[0].run_id)) == 1
    with pytest.raises(RunConflict):
        repository.create(scope, "changed", "same-key")


@pytest.mark.parametrize(
    "other",
    [
        Scope("tenant-b", "space-a", "user-a"),
        Scope("tenant-a", "space-b", "user-a"),
        Scope("tenant-a", "space-a", "user-b"),
    ],
)
def test_submission_keys_do_not_cross_identity_scope(repository, scope, other):
    first = repository.create(scope, "private", "same-key")
    second = repository.create(other, "different", "same-key")
    assert first.run_id != second.run_id


def test_without_key_each_submission_is_independent(repository, scope):
    assert repository.create(scope, "same").run_id != repository.create(scope, "same").run_id


@pytest.mark.parametrize("key", ["", "contains space", "x" * 129, "slash/key"])
def test_invalid_submission_key_is_rejected(client, headers, key):
    response = client.post(
        "/api/v1/runs",
        json={"prompt": "test"},
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 422
