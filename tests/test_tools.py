import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import Scope
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import (
    BUILTIN_TOOLS,
    DEFAULT_TOOLS,
    ReportResult,
    TextArguments,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    allowed_tools_from_env,
)
from agentforge_worker.tools import RunToolJournal
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call


def executor_for(repository, claim, **kwargs):
    return ToolExecutor(
        journal=RunToolJournal(repository, claim),
        checkpoint=lambda: repository.owns_claim(claim),
        **kwargs,
    )


def fake_registry(handler, *, effect="pure", version=1):
    return ToolRegistry(
        [
            ToolDefinition(
                "example_report",
                version,
                "test tool",
                TextArguments,
                ReportResult,
                handler,
                effect=effect,
                render=lambda output: output.text,
            )
        ]
    )


def test_catalog_agent_selection_and_mock_execution(client, headers, repository, scope):
    assert client.get("/api/v1/tools").status_code == 401
    catalog = client.get("/api/v1/tools", headers=headers).json()["items"]
    assert {tool["reference"] for tool in catalog} == set(DEFAULT_TOOLS)
    assert all(tool["effect"] == "pure" for tool in catalog)
    assert catalog[0]["input_schema"]["additionalProperties"] is False
    agent = client.post(
        "/api/v1/agents", json={"name": "统计助手", "tools": ["text_stats@1"]}, headers=headers
    )
    assert agent.status_code == 201
    thread = client.post(
        "/api/v1/threads", json={"agent_id": agent.json()["agent_id"]}, headers=headers
    ).json()
    run = client.post(
        "/api/v1/runs",
        json={"thread_id": thread["thread_id"], "prompt": "你好 world\nnext"},
        headers=headers,
    ).json()
    assert Worker(repository).run_once()
    history = client.get(f"/api/v1/runs/{run['run_id']}/tool-calls", headers=headers).json()
    assert len(history["items"]) == 1
    call = history["items"][0]
    assert call["name"] == "text_stats" and call["version"] == 1 and call["status"] == "succeeded"
    assert call["arguments"] == {"text": "你好 world\nnext"}
    assert call["output"] == {
        "characters": 13,
        "non_whitespace_characters": 11,
        "lines": 2,
        "whitespace_tokens": 3,
    }
    assert "tenant_id" not in call
    events = repository.list_events(scope, run["run_id"])
    assert [e.event_type for e in events] == [
        "queued",
        "running",
        "tool_started",
        "tool_completed",
        "succeeded",
    ]
    assert "你好" not in json.dumps([e.data for e in events], ensure_ascii=False)
    assert (
        client.get(
            f"/api/v1/runs/{run['run_id']}/tool-calls?after={history['next_cursor']}",
            headers=headers,
        ).json()["items"]
        == []
    )


@pytest.mark.parametrize(
    "refs",
    [
        ["shell@1"],
        ["example_report@2"],
        ["example_report@1", "example_report@1"],
        ["example_report@1", "example_report@2"],
        ["text_stats"],
        ["text_stats@0"],
    ],
)
def test_unknown_invalid_or_duplicate_versions_cannot_be_granted(client, headers, refs):
    assert (
        client.post(
            "/api/v1/agents", json={"name": "bad", "tools": refs}, headers=headers
        ).status_code
        == 422
    )


def test_current_policy_limits_catalog_and_agent_grants(settings, repository, headers):
    with TestClient(create_app(settings, repository, allowed_tools=("text_stats@1",))) as client:
        assert [
            item["reference"]
            for item in client.get("/api/v1/tools", headers=headers).json()["items"]
        ] == ["text_stats@1"]
        assert (
            client.post(
                "/api/v1/agents",
                json={"name": "bad", "tools": ["example_report@1"]},
                headers=headers,
            ).status_code
            == 422
        )
        agent = client.post(
            "/api/v1/agents", json={"name": "ok", "tools": ["text_stats@1"]}, headers=headers
        ).json()
        assert (
            client.put(
                "/api/v1/agents/" + agent["agent_id"],
                json={"name": "bad", "expected_version": 1, "tools": ["example_report@1"]},
                headers=headers,
            ).status_code
            == 422
        )


def test_revoked_permission_blocks_model_before_network(repository, scope):
    snapshot = ExecutionSnapshot(model=model_settings().snapshot())
    run = repository.create(scope, "revoked", execution_config=snapshot)
    requests = []
    worker = Worker(
        repository,
        allowed_tools=(),
        harness_factory=lambda config: configured_harness(
            model_settings().for_snapshot(config.model),
            transport=httpx.MockTransport(lambda r: requests.append(r)),
        ),
    )
    worker.run_once()
    assert requests == []
    assert repository.get(scope, run.run_id).error_code == "tool_not_allowed"
    assert repository.list_tool_calls(scope, run.run_id) == []


def test_empty_tool_selection_sends_plain_chat_without_function_fields(repository, scope):
    snapshot = ExecutionSnapshot(model=model_settings().snapshot(), tools=())
    run = repository.create(scope, "plain", execution_config=snapshot)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        Worker(repository, harness).run_once()
    assert "tools" not in requests[0] and "tool_choice" not in requests[0]
    assert repository.get(scope, run.run_id).status == "succeeded"
    assert repository.list_tool_calls(scope, run.run_id) == []


def test_chat_uses_multiple_registered_tools_and_persists_each_result(repository, scope):
    run = repository.create(
        scope,
        "two tools",
        execution_config=ExecutionSnapshot(model=model_settings().snapshot(), tools=DEFAULT_TOOLS),
    )
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert {tool["function"]["name"] for tool in body["tools"]} == {
            "example_report",
            "text_stats",
        }
        if len(requests) == 1:
            return httpx.Response(
                200, json=completion(None, [tool_call("r"), tool_call("s", name="text_stats")])
            )
        return httpx.Response(200, json=completion())

    with configured_harness(model_settings(), transport=httpx.MockTransport(respond)) as harness:
        Worker(repository, harness).run_once()
    records = repository.list_tool_calls(scope, run.run_id)
    assert [item["status"] for item in records] == ["succeeded", "succeeded"]
    assert [item["call_id"] for item in records] == ["r", "s"]
    assert json.loads(requests[1]["messages"][-1]["content"])["characters"] == 4
    assert records[1]["output"]["characters"] == 4


@pytest.mark.parametrize(
    "other",
    [
        Scope("b", "space-a", "user-a"),
        Scope("tenant-a", "b", "user-a"),
        Scope("tenant-a", "space-a", "b"),
    ],
)
def test_tool_records_never_cross_identity_scope(settings, repository, scope, headers, other):
    run = repository.create(scope, "private tool input")
    Worker(repository).run_once()
    with TestClient(create_app(replace(settings, dev_scope=other), repository)) as client:
        response = client.get(f"/api/v1/runs/{run.run_id}/tool-calls", headers=headers)
        assert response.status_code == 404
        assert "private tool input" not in response.text


def test_same_completed_invocation_returns_cached_result_without_execution(repository, scope):
    run = repository.create(scope, "deduplicate")
    claim = repository.claim_next()
    executed = []
    registry = fake_registry(lambda args: executed.append(args.text) or {"text": args.text})
    executor = executor_for(repository, claim, registry=registry)
    prepared = executor.prepare("same", "example_report", '{"text":"hello"}')
    assert executor.execute(prepared) == "hello"
    equivalent = executor.prepare("same", "example_report", '{ "text" : "hello" }')
    assert executor.execute(equivalent) == "hello"
    assert executed == ["hello"]
    assert len(repository.list_tool_calls(scope, run.run_id)) == 1
    changed = executor.prepare("same", "example_report", '{"text":"different"}')
    with pytest.raises(RuntimeFailure, match="tool_call_conflict"):
        executor.execute(changed)
    assert len(repository.list_events(scope, run.run_id)) == 4


def test_concurrent_duplicate_invocation_only_enters_handler_once(repository, scope):
    repository.create(scope, "in flight")
    claim = repository.claim_next()
    entered = Event()
    release = Event()
    calls = []

    def handler(args):
        calls.append(args.text)
        entered.set()
        assert release.wait(5)
        return {"text": args.text}

    executor = executor_for(repository, claim, registry=fake_registry(handler))
    prepared = executor.prepare("same", "example_report", '{"text":"once"}')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executor.execute, prepared)
        try:
            assert entered.wait(2)
            with pytest.raises(RuntimeFailure, match="tool_call_conflict"):
                executor.execute(prepared)
        finally:
            release.set()
        assert first.result(timeout=5) == "once"
    assert calls == ["once"]


@pytest.mark.parametrize("reason", ["cancel", "expire"])
def test_interrupted_tool_cannot_publish_result_and_is_abandoned(repository, scope, clock, reason):
    run = repository.create(scope, "interruption")

    def handler(args):
        if reason == "cancel":
            repository.cancel(scope, run.run_id)
        else:
            clock.advance(61)
            assert repository.claim_next() is not None
        return {"text": "late result"}

    registry = fake_registry(handler)
    Worker(repository, tool_registry=registry, allowed_tools=("example_report@1",)).run_once()
    records = repository.list_tool_calls(scope, run.run_id)
    assert records[0]["status"] == "abandoned"
    assert records[0]["output"] is None and records[0]["content"] is None
    assert not any(
        event.event_type == "tool_completed" for event in repository.list_events(scope, run.run_id)
    )


def test_new_attempt_has_separate_tool_records_and_stale_attempt_cannot_write(
    repository, scope, clock
):
    run = repository.create(scope, "recovery")
    old = repository.claim_next()
    first = executor_for(repository, old)
    prepared = first.prepare("same", "example_report", '{"text":"data"}')
    ticket = repository.start_tool(old, prepared.invocation)
    clock.advance(61)
    new = repository.claim_next()
    assert not repository.complete_tool(old, ticket, output={"text": "stale"}, content="stale")
    second = executor_for(repository, new)
    result = second.execute(second.prepare("same", "example_report", '{"text":"data"}'))
    repository.finish(new, result=result)
    records = repository.list_tool_calls(scope, run.run_id)
    assert [row["status"] for row in records] == ["abandoned", "succeeded"]
    assert records[0]["attempt_id"] != records[1]["attempt_id"]


@pytest.mark.parametrize(
    "failure,code", [("exception", "tool_execution_failed"), ("output", "tool_invalid_result")]
)
def test_tool_failures_are_recorded_without_private_exception_details(
    repository, scope, caplog, failure, code
):
    run = repository.create(scope, "private-input")

    def handler(_):
        if failure == "exception":
            raise RuntimeError("private-secret-token")
        return {"text": 123, "private-secret-token": "bad"}

    Worker(
        repository, tool_registry=fake_registry(handler), allowed_tools=("example_report@1",)
    ).run_once()
    assert repository.get(scope, run.run_id).error_code == code
    record = repository.list_tool_calls(scope, run.run_id)[0]
    assert (
        record["status"] == "failed" and record["error_code"] == code and record["output"] is None
    )
    assert "private-secret-token" not in json.dumps(dict(record))
    assert "private-input" not in caplog.text and "private-secret-token" not in caplog.text
    assert [event.event_type for event in repository.list_events(scope, run.run_id)] == [
        "queued",
        "running",
        "tool_started",
        "tool_failed",
        "failed",
    ]


@pytest.mark.parametrize("stage", ["start", "complete"])
def test_tool_record_and_event_update_roll_back_together(repository, scope, monkeypatch, stage):
    import agentforge_storage.repository as module

    run = repository.create(scope, "atomic")
    claim = repository.claim_next()
    executor = executor_for(repository, claim)
    invocation = executor.prepare("atomic", "example_report", '{"text":"data"}').invocation
    ticket = repository.start_tool(claim, invocation) if stage == "complete" else None

    def fail(*_):
        raise RuntimeError("storage failure")

    monkeypatch.setattr(module, "_event", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        if stage == "start":
            repository.start_tool(claim, invocation)
        else:
            repository.complete_tool(claim, ticket, output={"text": "data"}, content="data")
    records = repository.list_tool_calls(scope, run.run_id)
    if stage == "start":
        assert records == []
        assert len(repository.list_events(scope, run.run_id)) == 2
    else:
        assert records[0]["status"] == "running" and records[0]["output"] is None
        assert len(repository.list_events(scope, run.run_id)) == 3


@pytest.mark.parametrize("effect", ["read", "write"])
def test_external_effect_tools_remain_disabled_until_approval_and_recovery_exist(effect):
    with pytest.raises(RuntimeFailure, match="tool_not_allowed"):
        ToolExecutor(registry=fake_registry(lambda _: {"text": "unsafe"}, effect=effect))


def test_version_resolution_never_falls_back_to_another_version():
    registry = fake_registry(lambda _: {"text": "version two"}, version=2)
    with pytest.raises(RuntimeFailure, match="tool_not_allowed"):
        ToolExecutor(registry=registry, allowed=("example_report@2",))
    selected = ToolExecutor(("example_report@2",), registry=registry, allowed=("example_report@2",))
    assert (
        selected.execute(selected.prepare("id", "example_report", '{"text":"input"}'))
        == "version two"
    )


def test_allowlist_configuration_can_disable_all_tools_but_rejects_typos(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ALLOWED_TOOLS", "")
    assert allowed_tools_from_env() == ()
    monkeypatch.setenv("AGENTFORGE_ALLOWED_TOOLS", "text_stats@1, example_report@1")
    assert set(allowed_tools_from_env()) == set(DEFAULT_TOOLS)
    monkeypatch.setenv("AGENTFORGE_ALLOWED_TOOLS", "shell@1")
    with pytest.raises(ValueError):
        allowed_tools_from_env()


def test_registry_rejects_duplicate_version():
    definitions = BUILTIN_TOOLS.select(("example_report@1",), DEFAULT_TOOLS)
    with pytest.raises(ValueError):
        ToolRegistry([definitions["example_report"], definitions["example_report"]])
