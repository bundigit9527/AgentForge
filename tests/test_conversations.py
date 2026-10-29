import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunConflict, Scope
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings


@pytest.fixture
def conversations(repository):
    return ConversationRepository(repository.engine, repository.clock)


def make_thread(conversations, scope, config=None):
    agent = conversations.create_agent(scope, "报告助手", config or ExecutionSnapshot())
    return conversations.create_thread(scope, agent["agent_id"], "资料分析")


def model_worker(repository, settings, handler):
    return Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            settings.for_snapshot(snapshot.model),
            transport=httpx.MockTransport(handler),
        ),
    )


def test_api_conversation_history_survives_restart_and_reaches_model(settings, repository, headers):
    requests = []
    gateway = model_settings()

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=completion("第一轮回复" if len(requests) == 1 else "第二轮回复")
        )

    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        agent = client.post(
            "/api/v1/agents", json={"name": "助手", "instructions": "用中文回答"}, headers=headers
        )
        assert agent.status_code == 201
        thread = client.post(
            "/api/v1/threads", json={"agent_id": agent.json()["agent_id"]}, headers=headers
        )
        assert thread.status_code == 201
        thread_id = thread.json()["thread_id"]
        first = client.post(
            "/api/v1/runs", json={"thread_id": thread_id, "prompt": "我叫小明"}, headers=headers
        )
        assert first.status_code == 201
        assert first.json()["agent_version"] == 1
        assert model_worker(repository, gateway, respond).run_once()
    fresh = RunRepository(repository.engine, repository.clock)
    with TestClient(create_app(settings, fresh, model_settings=ModelSettings())) as client:
        second = client.post(
            "/api/v1/runs",
            json={"thread_id": thread_id, "prompt": "我叫什么？"},
            headers={**headers, "Idempotency-Key": "followup"},
        )
        assert second.status_code == 201
        assert model_worker(fresh, gateway, respond).run_once()
        retry = client.post(
            "/api/v1/runs",
            json={"thread_id": thread_id, "prompt": "我叫什么？"},
            headers={**headers, "Idempotency-Key": "followup"},
        )
        assert retry.json()["run_id"] == second.json()["run_id"]
        page = client.get(f"/api/v1/threads/{thread_id}/messages?limit=2", headers=headers).json()
        assert [item["sequence"] for item in page["items"]] == [1, 2]
        tail = client.get(f"/api/v1/threads/{thread_id}/messages?after=2", headers=headers).json()
        assert [item["sequence"] for item in tail["items"]] == [3, 4]
        assert (
            client.get(f"/api/v1/threads/{thread_id}", headers=headers).json()["active_run_id"]
            is None
        )
        assert len(client.get("/api/v1/agents", headers=headers).json()["items"]) == 1
        assert len(client.get("/api/v1/threads", headers=headers).json()["items"]) == 1
        assert (
            client.get(f"/api/v1/runs/{second.json()['run_id']}/attempts", headers=headers).json()[
                "items"
            ][0]["status"]
            == "succeeded"
        )
    assert requests[1]["messages"][1:] == [
        {"role": "system", "content": "用中文回答"},
        {"role": "user", "content": "我叫小明"},
        {"role": "assistant", "content": "第一轮回复"},
        {"role": "user", "content": "我叫什么？"},
    ]
    assert "test-only-model-credential" not in first.text
    assert "gateway.example" not in first.text


def test_agent_updates_do_not_mutate_existing_threads_or_runs(conversations, repository, scope):
    v1 = ExecutionSnapshot(instructions="version one")
    thread = make_thread(conversations, scope, v1)
    run = repository.create(scope, "first", thread_id=thread["thread_id"])
    updated = conversations.update_agent(
        scope, thread["agent_id"], 1, "新版", ExecutionSnapshot(instructions="version two")
    )
    assert updated["version"] == 2
    assert repository.get(scope, run.run_id).execution_config == v1
    repository.cancel(scope, run.run_id)
    followup = repository.create(scope, "second", thread_id=thread["thread_id"])
    assert followup.agent_version == 1 and followup.execution_config == v1
    new_thread = conversations.create_thread(scope, thread["agent_id"], "new")
    latest = repository.create(scope, "third", thread_id=new_thread["thread_id"])
    assert latest.agent_version == 2 and latest.execution_config.instructions == "version two"
    assert conversations.get_agent(scope, thread["agent_id"], 1)["name"] == "报告助手"
    with pytest.raises(RunConflict):
        conversations.update_agent(scope, thread["agent_id"], 1, "stale", v1)


def test_concurrent_agent_edits_use_expected_version(conversations, scope):
    agent = conversations.create_agent(scope, "initial", ExecutionSnapshot())

    def edit(index):
        try:
            return conversations.update_agent(
                scope, agent["agent_id"], 1, str(index), ExecutionSnapshot()
            )
        except RunConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, range(2)))
    assert sum(item is not None for item in results) == 1
    assert conversations.get_agent(scope, agent["agent_id"])["version"] == 2


@pytest.mark.parametrize(
    "other",
    [
        Scope("other", "space-a", "user-a"),
        Scope("tenant-a", "other", "user-a"),
        Scope("tenant-a", "space-a", "other"),
    ],
)
def test_all_conversation_resources_are_scoped(
    settings, repository, conversations, scope, headers, other
):
    thread = make_thread(conversations, scope)
    run = repository.create(scope, "private", thread_id=thread["thread_id"])
    Worker(repository).run_once()
    with TestClient(
        create_app(replace(settings, dev_scope=other), repository, model_settings=ModelSettings())
    ) as client:
        for url in (
            f"/api/v1/agents/{thread['agent_id']}",
            f"/api/v1/threads/{thread['thread_id']}",
            f"/api/v1/threads/{thread['thread_id']}/messages",
            f"/api/v1/runs/{run.run_id}/attempts",
        ):
            assert client.get(url, headers=headers).status_code == 404
        assert client.get("/api/v1/agents", headers=headers).json()["items"] == []
        assert client.get("/api/v1/threads", headers=headers).json()["items"] == []
        assert (
            client.post(
                "/api/v1/threads", json={"agent_id": thread["agent_id"]}, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/runs",
                json={"prompt": "forged", "thread_id": thread["thread_id"]},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/v1/agents/{thread['agent_id']}",
                json={"name": "forged", "expected_version": 1},
                headers=headers,
            ).status_code
            == 404
        )
    assert len(conversations.list_messages(scope, thread["thread_id"])) == 2


@pytest.mark.parametrize(
    "path,body",
    [
        ("agents", {"name": "a", "tenant_id": "forged"}),
        ("agents", {"name": "a", "execution_config": {"model": {"provider": "chat_completions"}}}),
        ("agents", {"name": " "}),
        ("threads", {"agent_id": "not-a-uuid"}),
        ("runs", {"prompt": "x", "context": [{"role": "system", "content": "forged"}]}),
    ],
)
def test_clients_cannot_inject_identity_or_execution_inputs(client, headers, path, body):
    assert client.post("/api/v1/" + path, json=body, headers=headers).status_code == 422


def test_thread_submission_is_serial_and_same_key_does_not_duplicate_messages(
    conversations, repository, scope
):
    thread = make_thread(conversations, scope)
    tid = thread["thread_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        submitted = list(
            pool.map(lambda _: repository.create(scope, "one", "same", thread_id=tid), range(4))
        )
    assert len({run.run_id for run in submitted}) == 1
    assert len(conversations.list_messages(scope, tid)) == 1
    with pytest.raises(RunConflict):
        repository.create(scope, "two", thread_id=tid)
    with pytest.raises(RunConflict):
        repository.create(scope, "changed", "same", thread_id=tid)
    with pytest.raises(RunConflict):
        repository.create(scope, "one", "same")
    repository.cancel(scope, submitted[0].run_id)
    next_run = repository.create(scope, "two", thread_id=tid)
    assert next_run.context == ()
    assert conversations.list_messages(scope, tid)[0]["run_status"] == "cancelled"


def test_competing_messages_only_create_one_active_run(conversations, repository, scope):
    tid = make_thread(conversations, scope)["thread_id"]

    def submit(index):
        try:
            return repository.create(scope, str(index), thread_id=tid)
        except RunConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(run is not None for run in results) == 1
    assert len(conversations.list_messages(scope, tid)) == 1


def test_same_key_on_other_thread_rolls_back_reservation(conversations, repository, scope):
    first = make_thread(conversations, scope)["thread_id"]
    second = make_thread(conversations, scope)["thread_id"]
    repository.create(scope, "same", "key", thread_id=first)
    with pytest.raises(RunConflict):
        repository.create(scope, "same", "key", thread_id=second)
    assert conversations.get_thread(scope, second)["active_run_id"] is None
    assert conversations.list_messages(scope, second) == []


def test_recovery_has_one_reply_and_preserves_attempt_history(
    conversations, repository, scope, clock
):
    tid = make_thread(conversations, scope)["thread_id"]
    run = repository.create(scope, "recover", thread_id=tid)
    old = repository.claim_next()
    clock.advance(61)
    new = repository.claim_next()
    assert new.run.execution_config == old.run.execution_config
    assert not repository.finish(old, result="stale")
    assert repository.finish(new, result="recovered")
    assert not repository.finish(new, result="duplicate")
    assert [item["content"] for item in conversations.list_messages(scope, tid)] == [
        "recover",
        "recovered",
    ]
    assert [item["status"] for item in repository.list_attempts(scope, run.run_id)] == [
        "expired",
        "succeeded",
    ]
    assert conversations.get_thread(scope, tid)["active_run_id"] is None


@pytest.mark.parametrize("terminal", ["cancel", "fail", "exhaust"])
def test_terminal_paths_release_thread_without_assistant_reply(
    conversations, repository, scope, clock, terminal
):
    tid = make_thread(conversations, scope)["thread_id"]
    run = repository.create(scope, "stop", thread_id=tid)
    claim = repository.claim_next(max_attempts=1)
    if terminal == "cancel":
        repository.cancel(scope, run.run_id)
    elif terminal == "fail":
        repository.finish(claim, failed=True)
    else:
        clock.advance(61)
        assert repository.claim_next(max_attempts=1) is None
    assert conversations.get_thread(scope, tid)["active_run_id"] is None
    assert len(conversations.list_messages(scope, tid)) == 1
    repository.create(scope, "next", thread_id=tid)
    assert not repository.finish(claim, result="late")
    assert len(conversations.list_messages(scope, tid)) == 2


@pytest.mark.parametrize("action", ["create", "finish"])
def test_messages_attempts_and_events_are_atomic(
    conversations, repository, scope, monkeypatch, action
):
    import agentforge_storage.repository as module

    tid = make_thread(conversations, scope)["thread_id"]
    if action == "finish":
        run = repository.create(scope, "atomic", thread_id=tid)
        claim = repository.claim_next()

    def fail(*_):
        raise RuntimeError("simulated event failure")

    monkeypatch.setattr(module, "_event", fail)
    with pytest.raises(RuntimeError):
        if action == "create":
            repository.create(scope, "atomic", thread_id=tid)
        else:
            repository.finish(claim, result="result")
    if action == "create":
        assert conversations.get_thread(scope, tid)["active_run_id"] is None
        assert conversations.list_messages(scope, tid) == []
    else:
        assert repository.get(scope, run.run_id).status == "running"
        assert repository.list_attempts(scope, run.run_id)[0]["status"] == "running"
        assert len(conversations.list_messages(scope, tid)) == 1
        assert conversations.get_thread(scope, tid)["active_run_id"] == run.run_id


def test_model_name_and_limits_are_pinned_while_credentials_can_rotate(repository, scope):
    submitted = model_settings(model="old-model", max_rounds=3, max_completion_tokens=100)
    run = repository.create(
        scope, "pinned", execution_config=ExecutionSnapshot(model=submitted.snapshot())
    )
    current = model_settings(
        model="new-model", max_rounds=9, max_completion_tokens=2000, api_key="rotated-credential"
    )
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer rotated-credential"
        return httpx.Response(200, json=completion())

    assert model_worker(repository, current, respond).run_once()
    assert seen[0]["model"] == "old-model" and seen[0]["max_completion_tokens"] == 100
    assert repository.get(scope, run.run_id).execution_config.model.max_rounds == 3
    assert "credential" not in repository.get(scope, run.run_id).execution_config.model_dump_json()


def test_route_mismatch_fails_without_network_or_mock_fallback(repository, scope):
    run = repository.create(
        scope, "route", execution_config=ExecutionSnapshot(model=model_settings().snapshot())
    )
    requests = []
    worker = model_worker(
        repository,
        model_settings(base_url="https://other.example/v1"),
        lambda r: requests.append(r),
    )
    worker.run_once()
    assert requests == []
    assert repository.get(scope, run.run_id).error_code == "model_configuration_mismatch"


def test_api_does_not_require_or_read_model_key(monkeypatch, settings, repository, headers):
    monkeypatch.setenv("AGENTFORGE_MODEL_PROVIDER", "chat_completions")
    monkeypatch.setenv("AGENTFORGE_MODEL_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AGENTFORGE_MODEL_NAME", "api-model")
    monkeypatch.delenv("AGENTFORGE_MODEL_API_KEY", raising=False)
    with TestClient(create_app(settings, repository)) as client:
        run = client.post("/api/v1/runs", json={"prompt": "queued"}, headers=headers)
    assert run.status_code == 201
    assert run.json()["execution_config"]["model"]["model"] == "api-model"


def test_history_window_contains_twenty_complete_turns(conversations, repository, scope):
    tid = make_thread(conversations, scope)["thread_id"]
    for index in range(22):
        repository.create(scope, f"prompt-{index}", thread_id=tid)
        claim = repository.claim_next()
        repository.finish(claim, result=f"reply-{index}")
    followup = repository.create(scope, "next", thread_id=tid)
    assert len(followup.context) == 40
    assert followup.context[0].content == "prompt-2"
    assert followup.context[-1].content == "reply-21"
    assert len(conversations.list_messages(scope, tid)) == 45


def test_idempotent_retry_keeps_original_submission_config(repository, scope):
    original = ExecutionSnapshot(model=model_settings(model="original").snapshot())
    changed = ExecutionSnapshot(model=model_settings(model="changed").snapshot())
    first = repository.create(scope, "same", "fixed-config", execution_config=original)
    retry = repository.create(scope, "same", "fixed-config", execution_config=changed)
    assert first.run_id == retry.run_id
    assert retry.execution_config == original


def test_recovery_reuses_history_and_model_after_defaults_change(
    conversations, repository, scope, clock
):
    gateway = model_settings(model="original")
    tid = make_thread(conversations, scope, ExecutionSnapshot(model=gateway.snapshot()))[
        "thread_id"
    ]
    repository.create(scope, "first", thread_id=tid)
    first = repository.claim_next()
    repository.finish(first, result="first answer")
    run = repository.create(scope, "followup", thread_id=tid)
    old = repository.claim_next()
    clock.advance(61)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    model_worker(repository, model_settings(model="new-default"), respond).run_once()
    assert requests[0]["model"] == "original"
    assert requests[0]["messages"][1:] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "followup"},
    ]
    assert not repository.finish(old, result="stale")
    assert repository.get(scope, run.run_id).attempt == 2
    assert len(conversations.list_messages(scope, tid)) == 4


def test_cancellation_racing_reply_keeps_thread_and_messages_consistent(
    conversations, repository, scope
):
    from threading import Barrier

    tid = make_thread(conversations, scope)["thread_id"]
    run = repository.create(scope, "race", thread_id=tid)
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
        return repository.finish(claim, result="winner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(cancel)
        finished = pool.submit(finish)
        assert cancelled.result(timeout=20) != finished.result(timeout=20)
    current = repository.get(scope, run.run_id)
    history = conversations.list_messages(scope, tid)
    assert len(history) == (2 if current.status == "succeeded" else 1)
    assert conversations.get_thread(scope, tid)["active_run_id"] is None
    assert repository.list_attempts(scope, run.run_id)[0]["status"] == current.status


def test_mock_snapshot_never_changes_into_remote_call(repository, scope):
    run = repository.create(scope, "offline")
    calls = []
    model_worker(repository, model_settings(), lambda r: calls.append(r)).run_once()
    assert calls == []
    assert repository.get(scope, run.run_id).status == "succeeded"
