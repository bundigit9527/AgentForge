"""Existing managed-model conversations must use an explicitly requested web search."""

import json

import httpx
import pytest
import sqlalchemy as sa
from agentforge_connectors import web
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_core.web import FetchInput
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import BUILTIN_TOOLS, DEFAULT_TOOLS, ToolExecutor
from agentforge_runtime.web_routing import requests_public_search
from agentforge_runtime.web_tools import with_web_tools
from agentforge_storage.schema import tool_calls
from agentforge_worker import worker as worker_module
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_model_management import connect
from tests.test_model_management import managed as _managed
from tests.test_model_management import people as _people
from tests.test_orpc import create_bot, rpc
from tests.test_web_tools import SEARCH

managed = _managed
people = _people


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("请检索今天的财经新闻，核对发布日期，并附来源链接", True),
        ("帮我检索下今天ETF规模最大的一支", True),
        ("帮忙检索比较好的智能体", True),
        ("帮我检索下好用的PPT skill", True),
        ("请检索上传的资料", False),
        ("请联网搜索 Python 官方文档", True),
        ("Please search the web for current financial news", True),
        ("不要联网搜索，只根据我提供的资料分析", False),
        ("请检索今天的财经新闻，只用附件，不要联网", False),
        ("Please search the attached local files for today's news", False),
        ("请检索知识库里的最新报告", False),
        ("请总结这句话：请检索今天新闻", False),
        ("为什么没有联网搜索工具？", False),
        ("帮我用电脑浏览器搜索今天的新闻", False),
        ("你好", False),
    ],
)
def test_only_explicit_current_public_search_is_required(prompt, expected):
    assert requests_public_search(prompt) is expected


def test_managed_model_searches_despite_stale_history_and_reads_chinese_page(
    managed, monkeypatch, tmp_path
):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    bot = create_bot(client, headers["alice"])
    worker = Worker(manager.store, model_manager=manager)
    stale = "我没有联网检索工具，无法主动查询今天的新闻。"
    upstream.action = lambda _: httpx.Response(200, json=completion(stale))
    for _ in range(3):
        assert (
            rpc(
                client,
                headers["alice"],
                "threads.send",
                {"botId": bot["id"], "text": "说明当前能力"},
            ).status_code
            == 200
        )
        worker.run_once()
    policy = tmp_path / "web.json"
    policy.write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "tenant_id": scopes["alice"].tenant_id,
                        "space_id": scopes["alice"].space_id,
                        "users": ["alice"],
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
        return (
            url,
            SEARCH
            if url.startswith(web.SEARCH_ENDPOINT)
            else "<main>" + "财经中文正文" * 3000 + "</main>",
            "text/html",
        )

    monkeypatch.setattr(
        worker_module,
        "WebService",
        lambda scope, snapshot, check: web.WebService(scope, snapshot, check, fetcher=fetch),
    )
    calls = []

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        assert (
            len(
                json.dumps({k: body[k] for k in ("messages", "tools")}, ensure_ascii=False).encode()
            )
            <= 28672
        )
        if len(calls) == 1:
            assert stale in json.dumps(body["messages"], ensure_ascii=False)
            assert body["tool_choice"] == {"type": "function", "function": {"name": "web_search"}}
            return httpx.Response(
                200, json=completion(None, [tool_call("s", "web_search", '{"query":"today news"}')])
            )
        assert body["tool_choice"] == "auto"
        if len(calls) == 2:
            return httpx.Response(
                200,
                json=completion(
                    None, [tool_call("f", "web_fetch", '{"url":"https://source.example/report"}')]
                ),
            )
        result = json.loads(body["messages"][-1]["content"])
        assert result["truncated"] and "财经中文正文" in result["text"]
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 28672 // 8
        return httpx.Response(200, json=completion("已读取公开来源：https://source.example/report"))

    upstream.action = model
    receipt = rpc(
        client,
        headers["alice"],
        "threads.send",
        {"botId": bot["id"], "text": "请检索今天的财经新闻，核对发布日期，并附来源链接"},
    ).json()["json"]
    worker.run_once()
    run = manager.store.get(scopes["alice"], receipt["runId"])
    assert run.status.value == "succeeded"
    assert len(run.context) == 6 and run.context[-1].content == stale
    with manager.store.engine.connect() as conn:
        records = conn.execute(
            sa.select(tool_calls.c.name, tool_calls.c.status)
            .where(tool_calls.c.run_id == run.run_id)
            .order_by(tool_calls.c.sequence)
        ).all()
    assert records == [("web_search", "succeeded"), ("web_fetch", "succeeded")]


def test_provider_ignoring_required_search_is_not_saved_as_a_success():
    settings = model_settings()
    snapshot = ExecutionSnapshot(
        model=settings.snapshot(), web_origins=("*",), web_search_endpoint=web.SEARCH_ENDPOINT
    )
    registry = with_web_tools(
        BUILTIN_TOOLS, web.WebService(None, snapshot, lambda: True), search=True
    )
    refs = ("web_search@1", "web_fetch@1")
    executor = ToolExecutor(
        refs, registry=registry, allowed=(*DEFAULT_TOOLS, *refs), read_allowed=refs
    )
    with configured_harness(
        settings,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion("没有联网工具"))
        ),
    ) as harness:
        with pytest.raises(RuntimeFailure) as error:
            harness.execute(
                "请检索今天的新闻",
                lambda _: True,
                execution_config=snapshot,
                tool_executor=executor,
            )
    assert error.value.code == RunErrorCode.MODEL_INVALID_RESPONSE


def test_utf8_web_output_is_bounded_with_escaped_text(monkeypatch):
    snapshot = ExecutionSnapshot(model=model_settings().snapshot(), web_origins=("*",))
    service = web.WebService(
        None,
        snapshot,
        lambda: True,
        fetcher=lambda url, guard: (url, '财经"\\' * 5000, "text/plain"),
    )
    monkeypatch.setattr(service, "guard", lambda _: None)
    result = service.fetch(FetchInput(url="https://source.example"))
    assert result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 18000


def test_managed_news_loop_trims_by_bytes_after_two_fetches(managed, monkeypatch, tmp_path):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    h, scope = headers["alice"], scopes["alice"]
    bot = create_bot(client, h)
    # Persist realistic Chinese history without spending model requests on setup.
    for _ in range(4):
        receipt = rpc(client, h, "threads.send", {"botId": bot["id"], "text": "旧" * 700}).json()[
            "json"
        ]
        claim = manager.store.claim_next()
        assert claim.run.run_id == receipt["runId"]
        manager.store.finish(claim, result="答" * 700)
    policy = tmp_path / "web.json"
    policy.write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "tenant_id": scope.tenant_id,
                        "space_id": scope.space_id,
                        "users": [scope.user_id],
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
        return (
            url,
            SEARCH
            if url.startswith(web.SEARCH_ENDPOINT)
            else "<main>" + "财经中文正文" * 3000 + "</main>",
            "text/html",
        )

    monkeypatch.setattr(
        worker_module,
        "WebService",
        lambda owner, snapshot, check: web.WebService(owner, snapshot, check, fetcher=fetch),
    )
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        encoded = json.dumps(
            {k: body[k] for k in ("messages", "tools")}, ensure_ascii=False
        ).encode()
        assert len(encoded) <= 28672
        if len(requests) == 1:
            assert body["tool_choice"]["function"]["name"] == "web_search"
            calls = [tool_call("s", "web_search", '{"query":"today finance"}')]
        elif len(requests) == 2:
            calls = [
                tool_call(f"f{i}", "web_fetch", json.dumps({"url": f"https://source.example/{i}"}))
                for i in range(2)
            ]
        else:
            outputs = [m for m in body["messages"] if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in outputs] == ["s", "f0", "f1"]
            return httpx.Response(200, json=completion("已核对：https://source.example/0"))
        return httpx.Response(200, json=completion(None, calls))

    upstream.action = model
    receipt = rpc(
        client,
        h,
        "threads.send",
        {"botId": bot["id"], "text": "请检索今天的财经新闻，核对发布日期，并附来源链接"},
    ).json()["json"]
    original = manager.store.get(scope, receipt["runId"])
    assert Worker(manager.store, model_manager=manager).run_once()
    final = manager.store.get(scope, original.run_id)
    assert final.status.value == "succeeded" and len(requests) == 3
    assert final.context == original.context and final.execution_config == original.execution_config
    trims = [
        e.data
        for e in manager.store.list_events(scope, original.run_id)
        if e.event_type == "context_trimmed"
    ]
    assert trims and any(e["step"] == 3 for e in trims)
    assert all(e["before_chars"] < e["budget_chars"] for e in trims)
    assert all(e["before_bytes"] > e["budget_bytes"] >= e["after_bytes"] for e in trims)
    assert [c["name"] for c in manager.store.list_tool_calls(scope, original.run_id)] == [
        "web_search",
        "web_fetch",
        "web_fetch",
    ]
