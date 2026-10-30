import httpx
import pytest
import sqlalchemy as sa
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunStatus
from agentforge_runtime.chat_harness import configured_harness
from agentforge_storage.schema import runs
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call


def test_model_tool_loop_persists_usage_and_resumable_events(client, headers, repository, scope):
    run_id = repository.create(
        scope, "summarize", execution_config=ExecutionSnapshot(model=model_settings().snapshot())
    ).run_id
    replies = iter([completion(None, [tool_call()]), completion()])
    with configured_harness(
        model_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(replies))),
    ) as harness:
        assert Worker(repository, harness).run_once()
    assert repository.get(scope, run_id).result == "最终回答"
    events = repository.list_events(scope, run_id)
    assert [event.event_type for event in events] == [
        "queued",
        "running",
        "model_completed",
        "tool_started",
        "tool_completed",
        "model_completed",
        "succeeded",
    ]
    assert [event.sequence for event in events] == list(range(1, 8))
    assert events[2].data == {
        "attempt": 1,
        "step": 1,
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    stream = client.get(
        f"/api/v1/runs/{run_id}/events/stream", headers={**headers, "Last-Event-ID": "4"}
    )
    assert "id: 5\n" in stream.text and "id: 6\n" in stream.text
    assert "id: 3\n" not in stream.text


@pytest.mark.parametrize(
    "failure,code",
    [
        ("timeout", "model_timeout"),
        ("auth", "model_auth_failed"),
        ("invalid", "model_invalid_response"),
        ("unknown_tool", "tool_not_allowed"),
    ],
)
def test_model_errors_have_public_codes_without_private_details(
    repository, scope, caplog, failure, code
):
    run = repository.create(
        scope,
        "private-prompt",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot()),
    )

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-provider-body", request=request)
        if failure == "auth":
            return httpx.Response(401, text="private-provider-body")
        if failure == "invalid":
            return httpx.Response(200, json={"private-provider-body": "invalid"})
        return httpx.Response(200, json=completion(None, [tool_call(name="shell")]))

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        assert Worker(repository, harness).run_once()
    result = repository.get(scope, run.run_id)
    assert result.status == RunStatus.FAILED
    assert result.error_code == code
    assert result.result is None
    assert not any(
        event.event_type == "tool_started" for event in repository.list_events(scope, run.run_id)
    )
    assert "private-provider-body" not in caplog.text
    assert "private-prompt" not in caplog.text
    assert "test-only-model-credential" not in caplog.text


def test_cancellation_during_model_request_discards_reply_and_stops_tools(repository, scope):
    run = repository.create(
        scope,
        "cancel while waiting",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot()),
    )
    requests = []

    def respond(request):
        requests.append(request)
        repository.cancel(scope, run.run_id)
        return httpx.Response(200, json=completion(None, [tool_call()]))

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        Worker(repository, harness).run_once()
    assert len(requests) == 1
    assert repository.get(scope, run.run_id).status == RunStatus.CANCELLED
    assert [event.event_type for event in repository.list_events(scope, run.run_id)] == [
        "queued",
        "running",
        "cancelled",
    ]


def test_expired_model_attempt_cannot_write_usage_and_can_be_reclaimed(repository, scope, clock):
    run = repository.create(
        scope,
        "expire while waiting",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot()),
    )

    def respond(_):
        clock.advance(61)
        return httpx.Response(200, json=completion())

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        Worker(repository, harness).run_once()
    assert repository.get(scope, run.run_id).status == RunStatus.RUNNING
    assert [event.event_type for event in repository.list_events(scope, run.run_id)] == [
        "queued",
        "running",
    ]
    with configured_harness(
        model_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
    ) as recovered_harness:
        Worker(repository, recovered_harness).run_once()
    result = repository.get(scope, run.run_id)
    assert result.status == RunStatus.SUCCEEDED and result.attempt == 2


def test_model_event_and_sequence_roll_back_on_event_storage_failure(
    repository, scope, monkeypatch
):
    import agentforge_storage.repository as module

    run = repository.create(
        scope,
        "event atomicity",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot()),
    )
    claim = repository.claim_next()

    def fail(*_):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(module, "_event", fail)
    with pytest.raises(RuntimeError):
        repository.record_model(claim, step=1, model="test-model", usage=None)
    with repository.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(runs.c.event_seq).where(runs.c.run_id == run.run_id)
            ).scalar_one()
            == 2
        )
    assert len(repository.list_events(scope, run.run_id)) == 2


def test_cancelled_claim_cannot_record_model_usage(repository, scope):
    run = repository.create(
        scope, "cancel", execution_config=ExecutionSnapshot(model=model_settings().snapshot())
    )
    claim = repository.claim_next()
    repository.cancel(scope, run.run_id)
    assert not repository.owns_claim(claim)
    assert not repository.record_model(claim, step=1, model="test-model", usage=None)


def test_worker_cli_selects_configured_model_without_network(
    monkeypatch, database_url, repository, scope
):
    import agentforge_worker.main as module

    run = repository.create(
        scope, "cli model", execution_config=ExecutionSnapshot(model=model_settings().snapshot())
    )
    seen = []

    def factory(settings):
        seen.append(settings.provider)
        return configured_harness(
            settings,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )

    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("AGENTFORGE_MODEL_PROVIDER", "chat_completions")
    monkeypatch.setenv("AGENTFORGE_MODEL_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AGENTFORGE_MODEL_NAME", "test-model")
    monkeypatch.setenv("AGENTFORGE_MODEL_API_KEY", "test-only-model-credential")
    monkeypatch.setattr(module, "configured_harness", factory)
    monkeypatch.setattr("sys.argv", ["agentforge-worker", "--once"])
    module.main()
    assert seen == ["chat_completions"]
    assert repository.get(scope, run.run_id).result == "最终回答"


def test_worker_cli_invalid_provider_does_not_silently_use_mock(
    monkeypatch, database_url, repository, scope
):
    import agentforge_worker.main as module

    run = repository.create(
        scope,
        "invalid provider",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot()),
    )
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("AGENTFORGE_MODEL_PROVIDER", "typo")
    monkeypatch.setattr("sys.argv", ["agentforge-worker", "--once"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert repository.get(scope, run.run_id).status == RunStatus.QUEUED
