import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_core.execution import ExecutionSnapshot
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_identity import share
from tests.test_orpc import create_bot, rpc
from tests.test_skills import hub as _hub
from tests.test_skills import install_body, recipe
from tests.test_skills import skill_client as _skill_client

enterprise = _enterprise
people = _people
hub = _hub
skill_client = _skill_client


def catalog(client, headers):
    response = rpc(client, headers, "agentSkills.list")
    assert response.status_code == 200, response.text
    return response.json()["json"]


def install(client, headers, hub):
    response = client.post("/api/v1/skills/install", headers=headers, json=install_body(hub))
    assert response.status_code == 201, response.text
    return response.json()["reference"]


def send(client, headers, bot_id, text, nonce="skill-message"):
    return rpc(
        client,
        headers,
        "threads.send",
        {
            "botId": bot_id,
            "text": text,
            "clientNonce": nonce,
        },
    )


def test_native_builtin_catalog_and_skill_markdown(client, headers):
    assert rpc(client, {}, "agentSkills.list").status_code == 401
    items = catalog(client, headers)
    assert [item["name"] for item in items] == ["资料摘要", "会议纪要", "文本统计", "PPT生成"]
    assert all(item["readOnly"] and item["source"] == "builtin" for item in items)
    assert all("content" not in item for item in items)
    detail = rpc(client, headers, "agentSkills.get", {"skillId": items[0]["id"]})
    assert detail.status_code == 200
    body = detail.json()["json"]
    assert body["content"].startswith('---\nname: "资料摘要"\n')
    assert "验收" in body["content"]
    assert body["createdAt"] == body["updatedAt"] == "1970-01-01T00:00:00Z"
    assert rpc(client, headers, "agentSkills.get", {"name": " 资料摘要 "}).json() == detail.json()
    for procedure in ("agentSkills.update", "agentSkills.remove"):
        body = {"skillId": items[0]["id"]}
        if procedure.endswith("update"):
            body["body"] = "must remain read-only"
        assert rpc(client, headers, procedure, body).status_code == 403
    # Demo/recorded playbooks are a different Rakazo subsystem, never fake recipe aliases.
    assert (
        rpc(
            client, headers, "skills.list", {"botId": create_bot(client, headers)["id"]}
        ).status_code
        == 501
    )


@pytest.mark.parametrize(
    "body,status",
    [
        ({}, 422),
        ({"name": None}, 422),
        ({"name": "资料摘要", "user_id": "forged"}, 422),
        ({"skillId": "../../private-file"}, 404),
        ({"name": "missing"}, 404),
    ],
)
def test_get_rejects_bad_or_unknown_targets(client, headers, body, status):
    assert rpc(client, headers, "agentSkills.get", body).status_code == status


def test_installed_versions_have_stable_names_and_are_read_only(skill_client, headers, hub):
    hub.document = recipe(name="😀" * 64)
    first = install(skill_client, headers, hub)
    original = next(item for item in catalog(skill_client, headers) if item["id"] == first)
    hub.document = recipe(version=2, name="😀" * 64, content="A newer recipe")
    second = install(skill_client, headers, hub)
    items = catalog(skill_client, headers)
    assert next(item for item in items if item["id"] == first) == original
    plugin = [item for item in items if item["source"] == "plugin"]
    assert len({item["name"] for item in plugin}) == 2
    assert {item["id"] for item in plugin} == {first, second}
    assert all(
        item["readOnly"] and len(item["name"].encode("utf-16-le")) // 2 <= 80 for item in plugin
    )
    detail = rpc(skill_client, headers, "agentSkills.get", {"name": original["name"].upper()})
    assert detail.status_code == 200
    assert detail.json()["json"]["id"] == first
    assert detail.json()["json"]["createdAt"] != "1970-01-01T00:00:00Z"
    assert original["name"] in detail.json()["json"]["content"]
    assert (
        skill_client.delete(f"/api/v1/skills/installations/{first}", headers=headers).status_code
        == 204
    )
    assert (
        rpc(skill_client, headers, "agentSkills.get", {"name": original["name"]}).status_code == 404
    )
    assert any(item["id"] == second for item in catalog(skill_client, headers))


@pytest.mark.parametrize("prefix", ["/", "Use skill: "])
def test_composer_choice_is_per_run_and_does_not_change_agent(
    client, headers, repository, scope, prefix
):
    bot = create_bot(client, headers)
    text = prefix + "资料摘要\n请整理这份资料"
    response = send(client, headers, bot["id"], text)
    assert response.status_code == 200, response.text
    run = repository.get(scope, response.json()["json"]["runId"])
    assert run.prompt == text
    assert run.execution_config.requested_skill == "builtin:document_summary@1"
    assert [s.reference for s in run.execution_config.skills] == ["builtin:document_summary@1"]
    assert run.execution_config.tools == ("example_report@1", "text_stats@1")
    agent = client.get(f"/api/v1/agents/{bot['id']}", headers=headers).json()
    assert agent["version"] == 1 and agent["execution_config"]["skills"] == []
    assert Worker(repository).run_once()
    calls = repository.list_tool_calls(scope, run.run_id)
    assert calls[0]["name"] == "skill_read"
    assert calls[0]["output"]["reference"] == "builtin:document_summary@1"
    assert send(client, headers, bot["id"], text).json() == response.json()
    following = send(client, headers, bot["id"], "plain message", "next")
    assert following.status_code == 200
    assert repository.get(scope, following.json()["json"]["runId"]).execution_config.skills == ()


def test_missing_tools_unknown_skill_and_forged_selection_leave_no_run(client, headers):
    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Restricted", "tools": ["example_report@1"]},
    ).json()
    bot = {"id": agent["agent_id"]}
    for text, status in [("/文本统计\nabc", 422), ("/missing recipe\nabc", 404)]:
        response = send(client, headers, bot["id"], text)
        assert response.status_code == status
        thread = rpc(client, headers, "threads.get", {"botId": bot["id"]}).json()["json"]
        assert thread["messages"] == [] and thread["run"] is None
    assert (
        rpc(
            client,
            headers,
            "threads.send",
            {
                "botId": bot["id"],
                "text": "abc",
                "skills": ["builtin:document_summary@1"],
            },
        ).status_code
        == 422
    )
    assert send(client, headers, bot["id"], "/会议纪要\nabc").status_code == 200


def test_owner_shared_skills_and_revocation(enterprise, repository, clock):
    client, headers, identities, scopes, hub = enterprise
    ref = install(client, headers["alice"], hub)
    entry = next(item for item in catalog(client, headers["alice"]) if item["id"] == ref)
    for user in ("admin", "bob", "viewer"):
        assert ref not in {item["id"] for item in catalog(client, headers[user])}
        assert rpc(client, headers[user], "agentSkills.get", {"skillId": ref}).status_code == 404
    bot = create_bot(client, headers["bob"])
    text = "/" + entry["name"] + "\nprivate recipe"
    assert send(client, headers["bob"], bot["id"], text).status_code == 404
    assert share(client, headers["alice"], "skill", ref).status_code == 200
    assert rpc(client, headers["viewer"], "agentSkills.get", {"skillId": ref}).status_code == 200
    response = send(client, headers["bob"], bot["id"], text)
    assert response.status_code == 200
    assert share(client, headers["bob"], "agent", bot["id"]).status_code == 200
    assert send(client, headers["viewer"], bot["id"], text).status_code == 403
    assert share(client, headers["alice"], "skill", ref, "private").status_code == 200
    assert rpc(client, headers["bob"], "agentSkills.get", {"skillId": ref}).status_code == 404
    store = RunRepository(repository.engine, clock, auth_mode="database")
    Worker(store).run_once()
    run = store.get(scopes["bob"], response.json()["json"]["runId"])
    assert run.status.value != "succeeded"
    assert store.list_tool_calls(scopes["bob"], run.run_id) == []
    identities.set_member(
        scopes["admin"], "alice", display_name="alice", role="member", active=False
    )
    assert rpc(client, headers["alice"], "agentSkills.list").status_code == 401


@pytest.mark.parametrize(
    "change", [{"tenant_id": "other"}, {"space_id": "other"}, {"user_id": "other"}]
)
def test_catalog_and_invocation_do_not_cross_identity_bounds(
    skill_client, headers, hub, settings, repository, change
):
    ref = install(skill_client, headers, hub)
    entry = next(item for item in catalog(skill_client, headers) if item["id"] == ref)
    other = replace(settings, dev_scope=replace(settings.dev_scope, **change))
    with TestClient(
        create_app(other, repository, model_settings=ModelSettings(), skill_hub=hub)
    ) as client:
        assert ref not in {item["id"] for item in catalog(client, headers)}
        assert rpc(client, headers, "agentSkills.get", {"skillId": ref}).status_code == 404
        assert (
            send(
                client, headers, create_bot(client, headers)["id"], "/" + entry["name"]
            ).status_code
            == 404
        )


def test_duplicate_send_reuses_snapshot_after_uninstall(
    skill_client, headers, hub, repository, scope, clock
):
    ref = install(skill_client, headers, hub)
    entry = next(item for item in catalog(skill_client, headers) if item["id"] == ref)
    bot = create_bot(skill_client, headers)
    text = "/" + entry["name"] + "\noriginal input"
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: send(skill_client, headers, bot["id"], text), range(2)))
    assert all(r.status_code == 200 for r in replies)
    assert replies[0].json() == replies[1].json()
    rid = replies[0].json()["json"]["runId"]
    saved = repository.get(scope, rid).execution_config
    abandoned = repository.claim_next(lease_seconds=1)
    assert abandoned.run.run_id == rid
    clock.advance(2)
    assert (
        skill_client.delete(f"/api/v1/skills/installations/{ref}", headers=headers).status_code
        == 204
    )
    hub.document = recipe(version=2, content="New version must not replace an old run")
    install(skill_client, headers, hub)
    assert send(skill_client, headers, bot["id"], text).json() == replies[0].json()
    assert send(skill_client, headers, bot["id"], text + " changed").status_code == 409
    assert Worker(repository).run_once()
    assert repository.get(scope, rid).execution_config == saved
    assert repository.get(scope, rid).status.value == "succeeded"
    assert send(skill_client, headers, bot["id"], text, "new-nonce").status_code == 404


def test_rest_slash_text_is_not_implicitly_bound(client, headers, repository, scope):
    bot = create_bot(client, headers)
    response = client.post(
        "/api/v1/runs",
        headers=headers,
        json={
            "thread_id": bot["threadId"],
            "prompt": "/资料摘要\nplain REST input",
        },
    )
    assert response.status_code == 201
    assert repository.get(scope, response.json()["run_id"]).execution_config.requested_skill is None
    with pytest.raises(ValidationError):
        ExecutionSnapshot(requested_skill="builtin:document_summary@1")


def test_selected_recipe_reaches_chat_completions_via_skill_reader(
    settings, repository, headers, scope
):
    gateway = model_settings()
    requests = []
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)
        response = send(client, headers, bot["id"], "/资料摘要\nprovided facts")
        assert response.status_code == 200
        rid = response.json()["json"]["runId"]

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json=completion(
                        None,
                        calls=[
                            tool_call(
                                name="skill_read",
                                arguments=json.dumps({"reference": "builtin:document_summary@1"}),
                            )
                        ],
                    ),
                )
            return httpx.Response(200, json=completion("Grounded summary"))

        worker = Worker(
            repository,
            harness_factory=lambda snapshot: configured_harness(
                gateway.for_snapshot(snapshot.model),
                transport=httpx.MockTransport(handler),
            ),
        )
        assert worker.run_once()
        assert repository.get(scope, rid).result == "Grounded summary"
        catalog_message = next(
            json.loads(message["content"])
            for message in requests[0]["messages"]
            if message["role"] == "user"
            and message["content"].startswith('{"requested_skill_reference"')
        )
        assert catalog_message["requested_skill_reference"] == "builtin:document_summary@1"
        tool_outputs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        assert "待核实事项" in tool_outputs[0]["content"]


def test_existing_agent_skills_are_preserved_and_not_duplicated(client, headers, repository, scope):
    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Prepared assistant",
            "skills": ["builtin:meeting_notes@1"],
        },
    ).json()
    response = send(client, headers, agent["agent_id"], "/资料摘要\ninput")
    assert response.status_code == 200
    saved = repository.get(scope, response.json()["json"]["runId"])
    assert [s.reference for s in saved.execution_config.skills] == [
        "builtin:document_summary@1",
        "builtin:meeting_notes@1",
    ]
    assert Worker(repository).run_once()
    response = send(client, headers, agent["agent_id"], "/会议纪要\ninput", "bound")
    saved = repository.get(scope, response.json()["json"]["runId"])
    assert len(saved.execution_config.skills) == 1
    assert saved.execution_config.requested_skill == "builtin:meeting_notes@1"


def test_skill_count_budget_cannot_be_bypassed_from_composer(skill_client, headers, hub):
    refs = []
    for n in range(8):
        hub.document = recipe(skill_id=f"recipe_{n}", name=f"Recipe {n}")
        refs.append(install(skill_client, headers, hub))
    agent = skill_client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Full catalog",
            "skills": refs,
        },
    )
    assert agent.status_code == 201
    response = send(skill_client, headers, agent.json()["agent_id"], "/资料摘要\ninput")
    assert response.status_code == 422
    thread = rpc(skill_client, headers, "threads.get", {"botId": agent.json()["agent_id"]}).json()[
        "json"
    ]
    assert thread["messages"] == [] and thread["run"] is None


def test_composer_does_not_replace_a_bound_version(skill_client, headers, hub):
    ref = install(skill_client, headers, hub)
    agent = skill_client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Versioned assistant",
            "skills": [ref],
        },
    ).json()
    hub.document = recipe(version=2)
    newer = install(skill_client, headers, hub)
    entry = next(item for item in catalog(skill_client, headers) if item["id"] == newer)
    assert send(skill_client, headers, agent["agent_id"], "/" + entry["name"]).status_code == 422
    original = skill_client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers).json()
    assert original["version"] == 1
    assert original["execution_config"]["skills"][0]["reference"] == ref


def test_deployment_tool_restriction_is_checked_before_enqueue(
    client, settings, headers, repository
):
    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Stats",
            "tools": ["text_stats@1"],
        },
    ).json()
    with TestClient(
        create_app(
            settings,
            repository,
            model_settings=ModelSettings(),
            allowed_tools=("example_report@1",),
        )
    ) as restricted:
        response = send(restricted, headers, agent["agent_id"], "/文本统计\ninput")
        assert response.status_code == 422
        snapshot = rpc(restricted, headers, "threads.get", {"botId": agent["agent_id"]}).json()[
            "json"
        ]
        assert snapshot["messages"] == [] and snapshot["run"] is None
