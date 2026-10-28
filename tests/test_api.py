from dataclasses import replace

import pytest
from agentforge_api.app import create_app
from agentforge_core.models import Scope
from agentforge_core.settings import Settings
from agentforge_storage.database import create_database
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient


def test_offline_task_round_trip_survives_new_repository(
    client, headers, repository, database_url, scope
):
    response = client.post("/api/v1/runs", json={"prompt": "生成一份示例报告"}, headers=headers)
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "queued"
    assert Worker(repository).run_once()
    result = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert result["status"] == "succeeded"
    assert "生成一份示例报告" in result["result"]
    engine = create_database(database_url)
    try:
        recovered = RunRepository(engine).get(scope, run_id)
        assert recovered.result == result["result"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "other_scope",
    [
        Scope("tenant-b", "space-a", "user-a"),
        Scope("tenant-a", "space-b", "user-a"),
        Scope("tenant-a", "space-a", "user-b"),
    ],
)
def test_scope_applies_to_read_cancel_and_events(
    client, headers, repository, settings, other_scope
):
    run_id = client.post("/api/v1/runs", json={"prompt": "private"}, headers=headers).json()[
        "run_id"
    ]
    with TestClient(create_app(replace(settings, dev_scope=other_scope), repository)) as other:
        for suffix in ("", "/events", "/events/stream"):
            assert other.get(f"/api/v1/runs/{run_id}{suffix}", headers=headers).status_code == 404
        assert other.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "queued"


@pytest.mark.parametrize("field", ["tenant_id", "space_id", "user_id", "status"])
def test_identity_and_state_cannot_be_injected(client, headers, field):
    response = client.post(
        "/api/v1/runs", json={"prompt": "test", field: "forged"}, headers=headers
    )
    assert response.status_code == 422


@pytest.mark.parametrize("prompt", ["", "  ", "a" * 10001])
def test_invalid_task_is_rejected(client, headers, prompt):
    assert client.post("/api/v1/runs", json={"prompt": prompt}, headers=headers).status_code == 422


def test_authentication_is_required_and_fails_closed(client, settings, repository):
    assert client.post("/api/v1/runs", json={"prompt": "test"}).status_code == 401
    assert (
        client.post(
            "/api/v1/runs", json={"prompt": "test"}, headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )
    with TestClient(create_app(replace(settings, dev_token=""), repository)) as disabled:
        assert disabled.post("/api/v1/runs", json={"prompt": "test"}).status_code == 503


def test_cancellation_is_idempotent_and_terminal_state_is_preserved(client, headers, repository):
    run_id = client.post("/api/v1/runs", json={"prompt": "cancel"}, headers=headers).json()[
        "run_id"
    ]
    for _ in range(2):
        assert (
            client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).json()["status"]
            == "cancelled"
        )
    assert not Worker(repository).run_once()
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers).json()["items"]
    assert [e["event_type"] for e in events] == ["queued", "cancelled"]


def test_finished_task_cannot_be_cancelled(client, headers, repository):
    run_id = client.post("/api/v1/runs", json={"prompt": "finish"}, headers=headers).json()[
        "run_id"
    ]
    Worker(repository).run_once()
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 409


def test_event_cursor_resumes_without_duplicate_events(client, headers, repository):
    run_id = client.post("/api/v1/runs", json={"prompt": "events"}, headers=headers).json()[
        "run_id"
    ]
    first = client.get(f"/api/v1/runs/{run_id}/events?limit=1", headers=headers).json()
    assert first["next_cursor"] == 1
    Worker(repository).run_once()
    second = client.get(f"/api/v1/runs/{run_id}/events?after=1", headers=headers).json()
    assert [e["event_type"] for e in second["items"]] == ["running", "tool_started", "succeeded"]
    empty = client.get(
        f"/api/v1/runs/{run_id}/events?after={second['next_cursor']}", headers=headers
    ).json()
    assert empty == {"items": [], "next_cursor": second["next_cursor"]}


def test_health_checks_do_not_create_schema(client, tmp_path):
    assert client.get("/health/ready").status_code == 200
    path = tmp_path / "uninitialized.db"
    app = create_app(Settings(f"sqlite+pysqlite:///{path}"))
    assert not path.exists()
    with TestClient(app) as uninitialized:
        assert uninitialized.get("/health/live").status_code == 200
        assert not path.exists()
        assert uninitialized.get("/health/ready").status_code == 503
