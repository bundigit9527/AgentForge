"""Long-page reading, historical capabilities and normal Worker tool execution."""

import json
from uuid import uuid4

import httpx
import pytest
from agentforge_api.desktop import Desktop
from agentforge_core.browser import (
    BROWSER_TOOLS,
    BrowserInput,
    BrowserReading,
    browser_result,
    browser_tools,
)
from agentforge_core.execution import ExecutionSnapshot
from agentforge_runtime.browser_tools import with_browser_tools
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.tools import BUILTIN_TOOLS
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.rakazo import RakazoRepository
from agentforge_supervisor import browser_pool
from agentforge_supervisor.browser_tab import document_hash, reading_page
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_visible_browser import PAGE, TOKEN, execute, submit
from tests.test_visible_browser import browser_env as _browser_env
from tests.test_visible_browser import people as _people

browser_env = _browser_env
people = _people


def document():
    return dict(
        url="https://example.com/",
        title="Long page",
        text="原文A😀" * 2000 + "精确定位目标" + " tail",
        links=[
            dict(id=str(i + 1), text=f"Link {i + 1}", url=f"https://example.com/{i}")
            for i in range(80)
        ],
        truncated=False,
        scroll_y=0,
        viewport_height=800,
        document_height=12000,
    )


def test_read_unicode_offsets_link_pagination_and_find():
    doc = document()
    first = reading_page(doc, {"action": "read"})
    second = reading_page(
        doc,
        {
            "action": "read",
            "offset": first["next_offset"],
            "links_offset": first["next_link_offset"],
        },
    )
    last = reading_page(doc, {"action": "read", "offset": second["next_offset"]})
    assert first["text"] + second["text"] + last["text"] == doc["text"]
    assert len(first["text"]) == 4000 and first["text"].count("😀") > 0
    assert last["next_offset"] is None
    assert second["links"][0]["id"] == "13" and second["total_links"] == 80
    found = reading_page(doc, {"action": "find", "text": "精确定位目标"})
    assert found["match_offset"] == 8000 and "精确定位目标" in found["text"]
    assert reading_page(doc, {"action": "find", "text": "missing"})["match_offset"] is None
    doc["truncated"] = True
    assert reading_page(doc, {"action": "read", "offset": 200000})["truncated"]


def test_document_identity_ignores_scroll_but_detects_tail_changes():
    doc = document()
    original = document_hash(doc)
    doc["scroll_y"] = 9000
    assert document_hash(doc) == original
    doc["text"] += "changed beyond first 4000"
    assert document_hash(doc) != original


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "read"},
        {"action": "read", "snapshot": "a" * 32, "offset": -1},
        {"action": "read", "snapshot": "a" * 32, "offset": True},
        {"action": "scroll", "snapshot": "a" * 32, "direction": "execute"},
        {"action": "find", "snapshot": "a" * 32, "text": " "},
        {"action": "read", "snapshot": "a" * 32, "url": "https://example.com/"},
        {"action": "follow", "snapshot": "a" * 32, "link": "1", "offset": 0},
    ],
)
def test_invalid_reading_inputs(arguments):
    with pytest.raises(ValueError):
        BrowserInput.model_validate(arguments)


def test_historical_snapshot_keeps_original_tool_set():
    config = ExecutionSnapshot(model=model_settings().snapshot(), browser_origins=("*",))
    assert not config.browser_reading
    assert browser_tools(config.browser_reading) == BROWSER_TOOLS
    old = with_browser_tools(BUILTIN_TOOLS, lambda _: None, ("*",))
    with pytest.raises(ValueError):
        old.validate_allowlist(("browser_read@1",))
    current = with_browser_tools(BUILTIN_TOOLS, lambda _: None, ("*",), reading=True)
    current.validate_allowlist(browser_tools(True))
    with pytest.raises(ValueError):
        ExecutionSnapshot(browser_reading=True)


def test_gateway_rejects_new_actions_for_old_run(browser_env, repository, people):
    client, _, _, _, calls, _, gateway = browser_env
    scope = people[1]["alice"]
    config = ExecutionSnapshot(
        model=gateway.snapshot(), browser_origins=("*",), authorization_mode="database"
    )
    agent = ConversationRepository(repository.engine, repository.clock).create_agent(
        scope, "Old", config
    )
    tid = RakazoRepository(repository).ensure_thread(scope, agent["agent_id"])
    repository.create(scope, "old input", thread_id=tid)
    claim = repository.claim_next()
    result = execute(client, claim, operation={"action": "read", "snapshot": "a" * 32})
    assert result.status_code == 403 and not calls


def test_worker_read_result_and_journal_roundtrip(browser_env, repository, people, monkeypatch):
    client, _, _, _, _, _, gateway = browser_env
    run = submit(browser_env)
    scope = people[1]["alice"]
    assert repository.get(scope, run).execution_config.browser_reading
    previous = Desktop.exchange

    def exchange(self, action, *params):
        if action == "browser" and params[1]["operation"]["action"] == "read":
            return {
                "result": {
                    **reading_page(document(), params[1]["operation"]),
                    "snapshot": uuid4().hex,
                }
            }
        return previous(self, action, *params)

    monkeypatch.setattr(Desktop, "exchange", exchange)

    def request(claim, args):
        result = execute(client, claim, operation=args)
        assert result.is_success, result.text
        return browser_result(args["action"], result.json())

    monkeypatch.setattr("agentforge_worker.worker.browser_request", request)
    replies = iter(
        [
            completion(
                None, [tool_call(name="browser_open", arguments=json.dumps({"url": PAGE["url"]}))]
            ),
            completion(
                None,
                [
                    tool_call(
                        call_id="read",
                        name="browser_read",
                        arguments=json.dumps(
                            {"snapshot": PAGE["snapshot"], "offset": 4000, "links_offset": 12}
                        ),
                    )
                ],
            ),
            completion("Read second segment"),
        ]
    )

    def model(request):
        body = json.loads(request.content)
        if (
            body["messages"][-1]["role"] == "tool"
            and "total_chars" in body["messages"][-1]["content"]
        ):
            result = BrowserReading.model_validate_json(body["messages"][-1]["content"])
            assert result.offset == 4000 and result.links[0].id == "13"
        return httpx.Response(200, json=next(replies))

    Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    ).run_once()
    assert repository.get(scope, run).result == "Read second segment"
    assert [x["status"] for x in repository.list_tool_calls(scope, run)] == [
        "succeeded",
        "succeeded",
    ]


def test_pool_stale_snapshot_and_observe_recovery(monkeypatch):
    monkeypatch.setenv("AGENTFORGE_BROWSER_TOKEN", TOKEN)
    client = httpx.Client
    monkeypatch.setattr(
        browser_pool.httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    pool = browser_pool.BrowserPool()
    pool.states["gen"] = {
        "identity": ("r", "a"),
        "state": {"context": "c", "target": "t"},
        "page": PAGE,
        "snapshot": PAGE["snapshot"],
    }

    def request(action, **args):
        return pool.execute(
            "gen",
            dict(
                runId="r",
                attemptId="a",
                origins=["*"],
                operationId="op",
                operation=dict(action=action, **args),
            ),
        )

    assert request("read", snapshot="b" * 32)["error"] == "browser_page_changed"

    def drive(gen, config, origins, check, saved):
        return reading_page(document(), config["operation"]), {"context": "c", "target": "t"}

    monkeypatch.setattr(pool, "drive", drive)
    read = request("read", snapshot=PAGE["snapshot"], links_offset=60)["result"]
    assert read["links"][0]["id"] == "61"
    assert (
        request("scroll", snapshot=PAGE["snapshot"], direction="down")["error"]
        == "browser_page_changed"
    )

    def changed(*args):
        raise browser_pool.BrowserPageChanged()

    monkeypatch.setattr(pool, "drive", changed)
    assert request("read", snapshot=read["snapshot"])["error"] == "browser_page_changed"

    def refresh(gen, config, *args):
        assert config["state"] == {"context": "c", "target": "t"}
        return {k: v for k, v in PAGE.items() if k != "snapshot"}, config["state"]

    monkeypatch.setattr(pool, "drive", refresh)
    assert request("observe")["result"]["url"] == PAGE["url"]


@pytest.mark.parametrize("failure", ["browser_page_changed", "browser_not_allowed", "browser_busy"])
def test_harness_refreshes_stale_browser_without_hiding_failed_call(
    browser_env, repository, people, monkeypatch, failure
):
    from agentforge_core.browser import BrowserFailure

    _, _, bot, _, _, _, gateway = browser_env
    run_id = submit(browser_env)
    requests = []

    def browser_request(claim, arguments):
        requests.append(arguments)
        if arguments["action"] == "follow":
            raise BrowserFailure(failure)
        return PAGE

    monkeypatch.setattr("agentforge_worker.worker.browser_request", browser_request)
    model_requests = []
    replies = iter(
        [
            completion(
                None,
                [tool_call("open", "browser_open", json.dumps({"url": "https://example.com/"}))],
            ),
            completion(
                None,
                [
                    tool_call(
                        "stale", "browser_follow", json.dumps({"snapshot": "b" * 32, "link": "1"})
                    )
                ],
            ),
            completion(None, [tool_call("refresh", "browser_observe", "{}")]),
            completion("Confirmed source: https://example.com/"),
        ]
    )

    def model(request):
        body = json.loads(request.content)
        model_requests.append(body)
        if len(model_requests) == 3:
            result = json.loads(body["messages"][-1]["content"])
            assert result["error"] == "browser_page_changed"
            assert "did not succeed" in result["message"]
            assert body["messages"][-1]["tool_call_id"] == "stale"
        return httpx.Response(200, json=next(replies))

    assert Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    ).run_once()
    owner = people[1]["alice"]
    journal = repository.list_tool_calls(owner, run_id)
    assert journal[1]["status"] == "failed" and journal[1]["error_code"] == failure
    run = repository.get(owner, run_id)
    if failure == "browser_page_changed":
        assert run.status.value == "succeeded"
        assert [r["action"] for r in requests] == ["open", "follow", "observe"]
        blocks = RakazoRepository(repository).snapshot(owner, bot["id"])["messages"][-1]["blocks"]
        assert any(b["kind"] == "meta" and failure in b["text"] for b in blocks)
        assert all(
            s["label"] != "browser_follow@1"
            for b in blocks
            if b["kind"] == "steps"
            for s in b["steps"]
        )
    else:
        assert run.status.value == "failed"
        assert len(model_requests) == 2 and len(requests) == 2


def test_stale_browser_recovery_obeys_model_round_limit(
    browser_env, repository, people, monkeypatch
):
    from agentforge_core.browser import BrowserFailure

    *_, gateway = browser_env
    run_id = submit(browser_env)
    count = 0

    def changed(*_):
        raise BrowserFailure("browser_page_changed")

    def model(_):
        nonlocal count
        count += 1
        return httpx.Response(
            200, json=completion(None, [tool_call(str(count), "browser_observe", "{}")])
        )

    monkeypatch.setattr("agentforge_worker.worker.browser_request", changed)
    assert Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    ).run_once()
    run = repository.get(people[1]["alice"], run_id)
    assert run.status.value == "failed" and run.error_code == "model_limit_exceeded"
    assert count == gateway.max_rounds
