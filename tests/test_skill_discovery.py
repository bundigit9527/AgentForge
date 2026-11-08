"""Skills discovered at submit time stay scoped, executable and version-pinned."""

import json

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_identity import share
from tests.test_model_management import connect
from tests.test_model_management import managed as _managed
from tests.test_orpc import create_bot, rpc
from tests.test_rpc_skills import install, send
from tests.test_user_skills import create

enterprise = _enterprise
people = _people
managed = _managed


def skill_catalog(body):
    return next(
        json.loads(m["content"])["available_skills"]
        for m in body["messages"]
        if m["role"] == "user" and m["content"].startswith('{"available_skills"')
    )


def test_ordinary_managed_chat_discovers_and_executes_a_frozen_personal_skill(managed):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    h, scope = headers["alice"], scopes["alice"]
    bot = create_bot(client, h)
    worker = Worker(manager.store, model_manager=manager)
    upstream.action = lambda _: httpx.Response(200, json=completion("此前没有可用技能"))
    assert send(client, h, bot["id"], "说明能力", "old").status_code == 200
    worker.run_once()
    item = create(
        client,
        h,
        content=(
            "---\nname: Exact statistics\ndescription: Count supplied text exactly\n"
            "required_tools: [text_stats@1]\n---\nUse text_stats and preserve whitespace."
        ),
    )
    text = "统计以下文本的字符数：a b\nc"
    receipt = send(client, h, bot["id"], text, "new")
    assert receipt.status_code == 200
    rid = receipt.json()["json"]["runId"]
    snapshot = manager.store.get(scope, rid).execution_config
    selected = next(s for s in snapshot.catalog_skills if s.source == "user")
    assert snapshot.skills == () and snapshot.requested_skill is None
    assert (
        rpc(
            client, h, "agentSkills.update", {"skillId": item["id"], "body": "New recipe"}
        ).status_code
        == 200
    )
    assert rpc(client, h, "agentSkills.remove", {"skillId": item["id"]}).status_code == 200
    assert send(client, h, bot["id"], text, "new").json() == receipt.json()
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["tool_choice"] == "auto"
        if len(bodies) == 1:
            assert "此前没有可用技能" in str(body["messages"])
            assert any(s["reference"] == selected.reference for s in skill_catalog(body))
            calls = [tool_call("read", "skill_read", json.dumps({"reference": selected.reference}))]
        elif len(bodies) == 2:
            assert "preserve whitespace" in body["messages"][-1]["content"]
            assert "New recipe" not in body["messages"][-1]["content"]
            calls = [tool_call("stats", "text_stats", json.dumps({"text": "a b\nc"}))]
        else:
            result = json.loads(body["messages"][-1]["content"])
            assert result["characters"] == 5 and result["lines"] == 2
            return httpx.Response(200, json=completion("5 个字符，2 行"))
        return httpx.Response(200, json=completion(None, calls))

    upstream.action = respond
    assert worker.run_once()
    run = manager.store.get(scope, rid)
    assert run.status.value == "succeeded" and run.execution_config == snapshot
    assert [c["name"] for c in manager.store.list_tool_calls(scope, rid)] == [
        "skill_read",
        "text_stats",
    ]


@pytest.mark.parametrize("wrong", ["final", "other_recipe", "other_tool", "extra_call", "bad_json"])
def test_explicit_skill_cannot_be_skipped_or_replaced(settings, repository, headers, scope, wrong):
    gateway = model_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)
        response = send(client, headers, bot["id"], "/资料摘要\nprovided facts")
        assert response.status_code == 200
        rid = response.json()["json"]["runId"]
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            assert body["tool_choice"] == {"type": "function", "function": {"name": "skill_read"}}
            call = tool_call("read", "skill_read", '{"reference":"builtin:document_summary@1"}')
            calls = [call]
            if wrong == "final":
                return httpx.Response(200, json=completion("Skipped the skill"))
            if wrong == "other_recipe":
                call["function"]["arguments"] = '{"reference":"builtin:meeting_notes@1"}'
            elif wrong == "other_tool":
                call["function"].update(name="text_stats", arguments='{"text":"abc"}')
            elif wrong == "extra_call":
                calls.append(tool_call("stats", "text_stats", '{"text":"abc"}'))
            else:
                call["function"]["arguments"] = "not-json"
            return httpx.Response(200, json=completion(None, calls))

        worker = Worker(
            repository,
            harness_factory=lambda snapshot: configured_harness(
                gateway.for_snapshot(snapshot.model), transport=httpx.MockTransport(respond)
            ),
        )
        assert worker.run_once()
        run = repository.get(scope, rid)
        assert (
            run.status.value == "failed" and run.error_code == RunErrorCode.MODEL_INVALID_RESPONSE
        )
        assert repository.list_tool_calls(scope, rid) == [] and len(requests) == 1


def test_explicit_statistic_skill_works_through_personal_route(managed):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    h, scope = headers["alice"], scopes["alice"]
    bot = create_bot(client, h)
    response = send(client, h, bot["id"], "/文本统计\nabc")
    assert response.status_code == 200
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert body["tool_choice"]["function"]["name"] == "skill_read"
            calls = [tool_call("read", "skill_read", '{"reference":"builtin:text_statistics@1"}')]
        elif len(bodies) == 2:
            assert body["tool_choice"] == "auto"
            calls = [tool_call("stats", "text_stats", '{"text":"abc"}')]
        else:
            assert json.loads(body["messages"][-1]["content"])["characters"] == 3
            return httpx.Response(200, json=completion("3 个字符"))
        return httpx.Response(200, json=completion(None, calls))

    upstream.action = respond
    assert Worker(manager.store, model_manager=manager).run_once()
    rid = response.json()["json"]["runId"]
    assert manager.store.get(scope, rid).status.value == "succeeded"
    assert [c["name"] for c in manager.store.list_tool_calls(scope, rid)] == [
        "skill_read",
        "text_stats",
    ]


def test_missing_dependency_is_named_and_not_auto_granted(client, headers, repository, scope):
    agent = client.post(
        "/api/v1/agents", headers=headers, json={"name": "Restricted", "tools": []}
    ).json()
    denied = send(client, headers, agent["agent_id"], "/文本统计\nabc")
    assert denied.status_code == 422
    assert "text_stats@1" in denied.json()["json"]["message"]
    plain = send(client, headers, agent["agent_id"], "hello", "plain")
    config = repository.get(scope, plain.json()["json"]["runId"]).execution_config
    assert config.tools == ()
    assert "builtin:text_statistics@1" not in {s.reference for s in config.available_skills}
    assert len(config.catalog_skills) == 2


def test_web_dependencies_resolved_after_scoped_grants(
    settings, repository, headers, scope, monkeypatch, tmp_path
):
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
    with TestClient(create_app(settings, repository, model_settings=model_settings())) as client:
        item = create(
            client,
            headers,
            content=(
                "---\nname: Search recipe\ndescription: Public research\n"
                "required_tools: [web_search@1, web_fetch@1]\n---\nSearch and verify sources."
            ),
        )
        bot = create_bot(client, headers)
        response = send(client, headers, bot["id"], "/" + item["name"])
        assert response.status_code == 200, response.text
        config = repository.get(scope, response.json()["json"]["runId"]).execution_config
        assert config.web_search_endpoint and config.skills[0].source == "user"
        assert "web_search@1" not in config.tools  # Still a separate scoped grant.


def test_catalog_is_private_and_shared_revocation_blocks_execution(enterprise, repository, clock):
    client, headers, _, scopes, hub = enterprise
    secret = create(client, headers["alice"], body="private recipe body")
    hub_ref = install(client, headers["alice"], hub)
    bob_bot = create_bot(client, headers["bob"])
    assert share(client, headers["alice"], "skill", hub_ref).status_code == 200
    alice_bot = create_bot(client, headers["alice"])
    alice_run = send(client, headers["alice"], alice_bot["id"], "hello").json()["json"]["runId"]
    bob_run = send(client, headers["bob"], bob_bot["id"], "hello").json()["json"]["runId"]
    alice_config = repository.get(scopes["alice"], alice_run).execution_config
    assert any(s.source_id == "user:" + secret["id"] for s in alice_config.catalog_skills)
    bob_config = repository.get(scopes["bob"], bob_run).execution_config
    assert all(s.source != "user" for s in bob_config.catalog_skills)
    assert hub_ref in {s.reference for s in bob_config.catalog_skills}
    assert share(client, headers["alice"], "run", alice_run).status_code == 200
    visible = client.get(f"/api/v1/runs/{alice_run}", headers=headers["bob"])
    assert visible.status_code == 200 and "private recipe body" not in visible.text
    assert visible.json()["execution_config"]["catalog_skills"] == []
    assert share(client, headers["alice"], "skill", hub_ref, "private").status_code == 200
    store = RunRepository(repository.engine, clock, auth_mode="database")
    worker = Worker(store)
    while worker.run_once():
        pass
    assert store.get(scopes["bob"], bob_run).status.value != "succeeded"
    assert store.list_tool_calls(scopes["bob"], bob_run) == []


def test_catalog_budget_explicit_selection_and_historical_defaults(
    client, headers, repository, scope
):
    for i in range(36):
        create(
            client, headers, name=f"Recipe {i}", description="x", body="Apply the supplied facts."
        )
    bot = create_bot(client, headers)
    response = send(client, headers, bot["id"], "hello")
    config = repository.get(scope, response.json()["json"]["runId"]).execution_config
    assert len(config.catalog_skills) <= 32 and config.catalog_omitted > 0
    assert (
        sum(
            len(json.dumps(s.catalog_entry(), ensure_ascii=False).encode()) + 2
            for s in config.catalog_skills
        )
        <= 8000
    )
    all_items = rpc(client, headers, "agentSkills.list").json()["json"]
    omitted = next(
        s
        for s in all_items
        if s["name"] not in {x.manifest.name for x in config.catalog_skills}
        and s["name"] != "PPT生成"
    )
    assert Worker(repository).run_once()
    selected = send(client, headers, bot["id"], "/" + omitted["name"], "explicit")
    assert selected.status_code == 200
    chosen = repository.get(scope, selected.json()["json"]["runId"]).execution_config
    assert chosen.skills[0].manifest.name == omitted["name"]
    legacy = ExecutionSnapshot.model_validate({"tools": []})
    assert legacy.available_skills == () and legacy.catalog_omitted == 0
    # Clients cannot inject their own catalog through the public submission schema.
    forged = rpc(
        client, headers, "threads.send", {"botId": bot["id"], "text": "x", "catalog_skills": []}
    )
    assert forged.status_code == 422


def test_cancelled_discovery_run_never_calls_a_model(managed):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    bot = create_bot(client, headers["alice"])
    rid = send(client, headers["alice"], bot["id"], "/资料摘要\nabc").json()["json"]["runId"]
    assert client.post(f"/api/v1/runs/{rid}/cancel", headers=headers["alice"]).status_code == 200
    count = len(upstream.requests)
    assert not Worker(manager.store, model_manager=manager).run_once()
    assert len(upstream.requests) == count
    assert manager.store.list_tool_calls(scopes["alice"], rid) == []


def test_new_bot_defaults_respect_deployment_allowlist(settings, repository, headers, scope):
    with TestClient(
        create_app(
            settings,
            repository,
            model_settings=ModelSettings(),
            allowed_tools=("example_report@1",),
        )
    ) as client:
        bot = create_bot(client, headers)
        response = send(client, headers, bot["id"], "/文本统计\nabc")
        assert response.status_code == 422 and "text_stats@1" in response.text
        agent = client.get(f"/api/v1/agents/{bot['id']}", headers=headers).json()
        assert agent["execution_config"]["tools"] == ["example_report@1"]


def test_explicit_skill_read_is_not_repeated_after_approval_resume(
    settings, repository, headers, scope
):
    from agentforge_storage.approvals import ApprovalRepository

    from tests.test_approval_resume import card_for

    gateway = model_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        agent = client.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "name": "Review statistics",
                "tools": ["text_stats@1"],
                "approval_tools": ["text_stats@1"],
            },
        ).json()
        receipt = send(client, headers, agent["agent_id"], "/文本统计\nabc")
        assert receipt.status_code == 200
        rid = receipt.json()["json"]["runId"]
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                assert body["tool_choice"]["function"]["name"] == "skill_read"
                calls = [
                    tool_call("read", "skill_read", '{"reference":"builtin:text_statistics@1"}')
                ]
            elif len(requests) == 2:
                assert body["tool_choice"] == "auto"
                calls = [tool_call("stats", "text_stats", '{"text":"abc"}')]
            else:
                assert body["tool_choice"] == "auto"
                assert json.loads(body["messages"][-1]["content"])["characters"] == 3
                return httpx.Response(200, json=completion("3"))
            return httpx.Response(200, json=completion(None, calls))

        worker = Worker(
            repository,
            harness_factory=lambda snapshot: configured_harness(
                gateway.for_snapshot(snapshot.model), transport=httpx.MockTransport(respond)
            ),
        )
        assert worker.run_once()
        paused = repository.get(scope, rid)
        assert paused.status.value == "waiting_input" and len(requests) == 2
        assert [c["name"] for c in repository.list_tool_calls(scope, rid)] == ["skill_read"]
        card = card_for(repository, paused)
        ApprovalRepository(repository).answer(scope, rid, card["message_id"], "allow")
        assert worker.run_once()
        assert repository.get(scope, rid).result == "3" and len(requests) == 3
        assert [c["name"] for c in repository.list_tool_calls(scope, rid)] == [
            "skill_read",
            "text_stats",
        ]
