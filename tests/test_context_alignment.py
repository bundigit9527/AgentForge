"""Rakazo model-window and tool-evidence projection behavior, including Worker audit."""

import json
from copy import deepcopy

import httpx
import pytest
from agentforge_connectors import web
from agentforge_core.execution import ModelConfig
from agentforge_core.model_limits import connect_window
from agentforge_core.model_management import ConnectModel
from agentforge_runtime.context import ContextWindow, context_bytes
from agentforge_runtime.errors import RuntimeFailure
from agentforge_worker import worker as worker_module
from agentforge_worker.worker import Worker

from tests.model_support import completion, tool_call
from tests.test_model_management import connect
from tests.test_model_management import managed as _managed
from tests.test_model_management import people as _people
from tests.test_orpc import create_bot, rpc

managed = _managed
people = _people


def test_catalog_window_and_explicit_override_preserve_old_run(managed):
    _, _, manager, scopes, _ = managed
    first = connect(managed, provider="deepseek", modelId="deepseek-v4-pro")
    assert first["contextWindow"] == 1_000_000
    config = manager.default(scopes["alice"]).snapshot()
    assert config.context_max_bytes == config.context_max_chars == 995904
    assert config.context_window_tokens == 1_000_000 and config.compact_tool_results
    # No residual 100k-character cap for catalog-backed managed models.
    window = ContextWindow([], (), "x" * 120000, [], config=config)
    assert window.fit()[1] is None
    legacy = ModelConfig.model_validate(
        {
            **config.model_dump(exclude={"compact_tool_results", "context_window_tokens"}),
            "context_max_bytes": 28672,
            "context_max_chars": 100000,
        }
    )
    assert not legacy.compact_tool_results
    assert manager.resolve(scopes["alice"], legacy).snapshot() == legacy
    connect(managed, provider="deepseek", modelId="deepseek-v4-pro", contextWindow=16384)
    assert manager.default(scopes["alice"]).snapshot().context_max_bytes == 12288
    saved = connect(managed, provider="deepseek", modelId="deepseek-flash", apiKey=None)
    assert saved["contextWindow"] == 16384
    custom = connect(managed, modelId="deepseek-v4-pro")
    assert custom["contextWindow"] == 32768  # A name cannot prove a gateway's capacity.


def test_legacy_and_unknown_limits_are_not_silently_expanded():
    body = ConnectModel(provider="deepseek", modelId="deepseek-v4-pro")
    assert connect_window(body, {"contextWindow": 32768}) == (32768, "explicit")
    assert connect_window(body, {}) == (1_000_000, "catalog")
    assert connect_window(body.model_copy(update={"modelId": "unknown"}), {}) == (32768, "default")


def group(call_id, name="web_fetch", text="新闻" * 3000):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [tool_call(call_id, name, '{"url":"https://source.example/article"}')],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {
                    "url": "https://source.example/article",
                    "artifact_id": "unchanged",
                    "path": "artifacts/file.pptx",
                    "status": "succeeded",
                    "exit_code": 0,
                    "retrievedAt": "2026-11-10T00:00:00Z",
                    "text": text,
                },
                ensure_ascii=False,
            ),
        },
    ]


def projection(**changes):
    return ContextWindow(
        [],
        (),
        "current question",
        [],
        config=ModelConfig(
            context_policy="trim_history",
            compact_tool_results=True,
            context_max_chars=100000,
            context_max_bytes=8000,
            **changes,
        ),
    )


def test_projection_keeps_protocol_controls_and_original_checkpoint():
    window = projection()
    window.current += group("old") + group("latest")
    original = deepcopy(window.current)
    messages, stats = window.fit()
    assert window.current == original
    assert context_bytes(messages, []) <= 8000
    assert stats["dropped_turns"] == 0 and stats["truncated_tool_results"] == 2
    for old, new in zip(original, messages, strict=True):
        if old["role"] != "tool":
            assert old == new
            continue
        a, b = json.loads(old["content"]), json.loads(new["content"])
        assert b.pop("contextTruncated") is True
        assert len(b.pop("text")) < len(a.pop("text"))
        assert a == b
    assert window.fit() == (messages, stats)  # Recovery uses the same raw checkpoint.


@pytest.mark.parametrize("protected", ["skill_read", "instructions", "arguments"])
def test_protected_content_fails_instead_of_corrupting_it(protected):
    window = projection()
    if protected == "instructions":
        window.prefix.append({"role": "system", "content": "守" * 10000})
    else:
        window.current += group("one", protected)
        if protected == "arguments":
            window.current[1]["tool_calls"][0]["function"]["arguments"] = json.dumps(
                {"code": "x" * 10000}
            )
    original = deepcopy(window.current)
    with pytest.raises(RuntimeFailure, match="model_context_limit_exceeded"):
        window.fit()
    assert window.current == original


def test_unpaired_result_is_never_sent():
    window = projection()
    window.current.append(group("orphan")[-1])
    with pytest.raises(RuntimeFailure, match="execution_failed"):
        window.fit()


def test_managed_worker_many_sources_compacts_and_keeps_full_journal(
    managed, monkeypatch, tmp_path
):
    client, headers, manager, scopes, upstream = managed
    connect(managed, contextWindow=22000)
    owner, h = scopes["alice"], headers["alice"]
    policy = tmp_path / "web.json"
    policy.write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "tenant_id": owner.tenant_id,
                        "space_id": owner.space_id,
                        "users": [owner.user_id],
                        "origins": ["*"],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("AGENTFORGE_WEB_ENABLED", "true")
    monkeypatch.setenv("AGENTFORGE_WEB_POLICY_FILE", str(policy))

    def fetch(url, guard):
        guard(url)
        return url, "<main>" + "新闻资料" * 10000 + "</main>", "text/html"

    monkeypatch.setattr(
        worker_module,
        "WebService",
        lambda scope, snapshot, check: web.WebService(scope, snapshot, check, fetcher=fetch),
    )
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert context_bytes(body["messages"], body["tools"]) <= 17904
        if len(requests) <= 3:
            calls = [
                tool_call(
                    f"f{len(requests)}-{i}",
                    "web_fetch",
                    json.dumps({"url": f"https://source.example/{len(requests)}/{i}"}),
                )
                for i in range(3)
            ]
            return httpx.Response(200, json=completion(None, calls))
        assert len([m for m in body["messages"] if m["role"] == "tool"]) == 9
        assert any(
            '"contextTruncated": true' in m.get("content", "")
            for m in body["messages"]
            if m["role"] == "tool"
        )
        return httpx.Response(200, json=completion("已根据来源完成核对"))

    upstream.action = model
    bot = create_bot(client, h)
    receipt = rpc(
        client, h, "threads.send", {"botId": bot["id"], "text": "请读取给定公开来源并核对资料"}
    ).json()["json"]
    before = manager.store.get(owner, receipt["runId"])
    assert Worker(manager.store, model_manager=manager).run_once()
    after = manager.store.get(owner, before.run_id)
    assert after.status.value == "succeeded" and len(requests) == 4, (
        after.error_code,
        len(requests),
        manager.store.list_tool_calls(owner, before.run_id),
    )
    assert before.execution_config == after.execution_config
    events = manager.store.list_events(owner, before.run_id)
    trims = [e.data for e in events if e.event_type == "context_trimmed"]
    assert any(e.get("truncated_tool_results", 0) > 0 and e["dropped_turns"] == 0 for e in trims)
    calls = manager.store.list_tool_calls(owner, before.run_id)
    assert len(calls) == 9
    assert all(c["status"] == "succeeded" and "contextTruncated" not in c["content"] for c in calls)


@pytest.mark.parametrize("stop", ["cancel", "expired"])
def test_projection_audit_rejects_stale_execution(repository, scope, clock, stop):
    run = repository.create(scope, "task")
    claim = repository.claim_next()
    if stop == "cancel":
        repository.cancel(scope, run.run_id)
    else:
        clock.advance(61)
    assert not repository.record_context(
        claim,
        step=1,
        budget_chars=100000,
        before_chars=6000,
        after_chars=2000,
        budget_bytes=8000,
        before_bytes=10000,
        after_bytes=4000,
        dropped_turns=0,
        retained_turns=0,
        truncated_tool_results=2,
    )
