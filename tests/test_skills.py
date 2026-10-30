import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_connectors.skill_hub import HttpSkillHub, SkillHubSettings
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.skills import HubCatalog, SkillManifest
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_skills.builtin import BUILTIN_SKILLS
from agentforge_storage.skills import SkillRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.model_support import completion, model_settings, tool_call


def recipe(**changes):
    return SkillManifest(
        **{
            "skill_id": "research_summary",
            "version": 1,
            "name": "研究摘要",
            "description": "仅整理已提供的资料",
            "content": "按事实、结论、待核实事项整理。",
            **changes,
        }
    )


def summary(manifest):
    return {**manifest.model_dump(mode="json", exclude={"content"}), "sha256": manifest.digest()}


class FakeHub:
    source_id = "hub:" + "a" * 64

    def __init__(self):
        self.document = recipe()
        self.requests = []

    def catalog(self, offset, limit, query):
        self.requests.append((offset, limit, query))
        return HubCatalog(items=[summary(self.document)] if offset == 0 else [])

    def manifest(self, skill_id, version):
        self.requests.append((skill_id, version))
        assert (skill_id, version) == (self.document.skill_id, self.document.version)
        return self.document


def install_body(hub):
    return {
        "source_id": hub.source_id,
        "skill_id": hub.document.skill_id,
        "version": hub.document.version,
        "sha256": hub.document.digest(),
    }


@pytest.fixture
def hub():
    return FakeHub()


@pytest.fixture
def skill_client(settings, repository, hub):
    with TestClient(
        create_app(settings, repository, model_settings=ModelSettings(), skill_hub=hub)
    ) as client:
        yield client


def test_default_skills_are_available_without_a_hub(client, headers):
    assert client.get("/api/v1/skills").status_code == 401
    page = client.get("/api/v1/skills", headers=headers).json()
    assert [s["manifest"]["name"] for s in page["items"]] == ["资料摘要", "会议纪要", "文本统计"]
    assert all(s["source"] == "builtin" for s in page["items"])
    assert page["next_offset"] is None
    assert client.get("/api/v1/skills/hub", headers=headers).status_code == 503
    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "摘要助手", "tools": [], "skills": [page["items"][0]["reference"]]},
    )
    assert agent.status_code == 201
    assert len(agent.json()["execution_config"]["skills"]) == 1


def test_browse_preview_install_and_paginate(skill_client, hub, headers):
    page = skill_client.get("/api/v1/skills/hub?q=研究&limit=5", headers=headers).json()
    assert page["source_id"] == hub.source_id
    assert hub.requests[-1] == (0, 5, "研究")
    assert len(skill_client.get("/api/v1/skills", headers=headers).json()["items"]) == 3
    preview = skill_client.get("/api/v1/skills/hub/research_summary/versions/1", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["sha256"] == hub.document.digest()
    installed = skill_client.post("/api/v1/skills/install", headers=headers, json=install_body(hub))
    assert installed.status_code == 201
    again = skill_client.post("/api/v1/skills/install", headers=headers, json=install_body(hub))
    assert again.json()["reference"] == installed.json()["reference"]
    first = skill_client.get("/api/v1/skills?limit=2", headers=headers).json()
    second = skill_client.get("/api/v1/skills?limit=2&offset=2", headers=headers).json()
    assert first["next_offset"] == 2 and second["next_offset"] is None
    assert second["items"][1] == installed.json()
    assert "tenant_id" not in installed.json()


@pytest.mark.parametrize("change", ["digest", "source", "published_version"])
def test_changed_source_or_content_is_not_silently_installed(skill_client, hub, headers, change):
    body = install_body(hub)
    if change == "digest":
        body["sha256"] = "f" * 64
    elif change == "source":
        body["source_id"] = "hub:" + "b" * 64
    else:
        assert (
            skill_client.post("/api/v1/skills/install", json=body, headers=headers).status_code
            == 201
        )
        hub.document = recipe(content="上游错误地修改同一版本")
        body = install_body(hub)
    assert (
        skill_client.post("/api/v1/skills/install", json=body, headers=headers).status_code == 409
    )


@pytest.mark.parametrize(
    "scope_changes",
    [
        {"tenant_id": "other"},
        {"space_id": "other"},
        {"user_id": "other"},
    ],
)
def test_installations_are_scoped_and_cannot_be_bound_by_another_user(
    skill_client,
    hub,
    headers,
    settings,
    repository,
    scope,
    scope_changes,
):
    installed = skill_client.post(
        "/api/v1/skills/install", json=install_body(hub), headers=headers
    ).json()
    reference = installed["reference"]
    other = replace(settings, dev_scope=replace(scope, **scope_changes))
    with TestClient(
        create_app(other, repository, model_settings=ModelSettings(), skill_hub=hub)
    ) as client:
        assert len(client.get("/api/v1/skills", headers=headers).json()["items"]) == 3
        assert (
            client.post(
                "/api/v1/agents", json={"name": "a", "skills": [reference]}, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/api/v1/skills/installations/{reference}", headers=headers).status_code
            == 404
        )
        own = client.post("/api/v1/skills/install", json=install_body(hub), headers=headers).json()
        assert own["reference"] != reference


@pytest.mark.parametrize(
    "body",
    [
        {"skills": ["builtin:text_statistics@1"]},
        {"tools": [], "skills": ["builtin:text_statistics@1"]},
        {"skills": ["builtin:document_summary@1", "builtin:document_summary@1"]},
        {"skills": ["../../secret"]},
        {"skills": [{"content": "injected", "source": "builtin"}]},
    ],
)
def test_skills_do_not_grant_tools_or_accept_arbitrary_snapshots(client, headers, body):
    result = client.post("/api/v1/agents", json={"name": "test", **body}, headers=headers)
    assert result.status_code == 422


def test_binding_unknown_builtin_fails(client, headers):
    assert (
        client.post(
            "/api/v1/agents", json={"name": "a", "skills": ["builtin:missing@1"]}, headers=headers
        ).status_code
        == 404
    )


def test_uninstall_only_removes_future_selection_and_reinstall_reuses_id(
    skill_client, hub, headers
):
    installed = skill_client.post(
        "/api/v1/skills/install", json=install_body(hub), headers=headers
    ).json()
    reference = installed["reference"]
    agent = skill_client.post(
        "/api/v1/agents", json={"name": "a", "skills": [reference]}, headers=headers
    ).json()
    assert (
        skill_client.delete(
            f"/api/v1/skills/installations/{reference}", headers=headers
        ).status_code
        == 204
    )
    assert len(skill_client.get("/api/v1/skills", headers=headers).json()["items"]) == 3
    assert (
        skill_client.post(
            "/api/v1/agents", json={"name": "new", "skills": [reference]}, headers=headers
        ).status_code
        == 404
    )
    unchanged = skill_client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers).json()
    assert unchanged["execution_config"]["skills"] == [installed]
    again = skill_client.post(
        "/api/v1/skills/install", json=install_body(hub), headers=headers
    ).json()
    assert again["reference"] == reference


def test_skill_snapshot_survives_update_uninstall_and_worker_recovery(
    settings,
    repository,
    hub,
    scope,
    headers,
    clock,
):
    gateway = model_settings()
    with TestClient(
        create_app(settings, repository, model_settings=gateway, skill_hub=hub)
    ) as client:
        installed = client.post(
            "/api/v1/skills/install", json=install_body(hub), headers=headers
        ).json()
        body = {"name": "a", "tools": [], "skills": [installed["reference"]]}
        agent = client.post("/api/v1/agents", json=body, headers=headers).json()
        thread = client.post(
            "/api/v1/threads", json={"agent_id": agent["agent_id"]}, headers=headers
        ).json()
        run = client.post(
            "/api/v1/runs",
            json={"thread_id": thread["thread_id"], "prompt": "请整理"},
            headers=headers,
        ).json()
        old = repository.claim_next()
        hub.document = recipe(version=2, content="第二版内容")
        newer = client.post(
            "/api/v1/skills/install", json=install_body(hub), headers=headers
        ).json()
        update = client.put(
            f"/api/v1/agents/{agent['agent_id']}",
            json={**body, "skills": [newer["reference"]], "expected_version": 1},
            headers=headers,
        )
        assert update.status_code == 200
        client.delete(f"/api/v1/skills/installations/{installed['reference']}", headers=headers)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    None,
                    calls=[
                        tool_call(
                            name="skill_read",
                            arguments=json.dumps({"reference": installed["reference"]}),
                        )
                    ],
                ),
            )
        return httpx.Response(200, json=completion("整理完成"))

    clock.advance(61)
    Worker(
        repository,
        harness_factory=lambda s: configured_harness(
            gateway.for_snapshot(s.model), transport=httpx.MockTransport(respond)
        ),
    ).run_once()
    assert not repository.finish(old, result="stale")
    current = repository.get(scope, run["run_id"])
    assert current.status == "succeeded" and current.attempt == 2
    assert current.execution_config.skills[0].manifest.version == 1
    serialized = json.dumps(requests[-1], ensure_ascii=False)
    assert "按事实、结论、待核实事项整理。" in serialized and "第二版内容" not in serialized
    assert "按事实、结论、待核实事项整理。" not in json.dumps(requests[0], ensure_ascii=False)
    assert [t["function"]["name"] for t in requests[0]["tools"]] == ["skill_read"]
    calls = repository.list_tool_calls(scope, run["run_id"])
    assert calls[0]["name"] == "skill_read" and calls[0]["status"] == "succeeded"


def test_builtin_recipe_with_tool_runs_through_common_executor(
    settings, repository, headers, scope
):
    gateway = model_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        agent = client.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "name": "统计",
                "tools": ["text_stats@1"],
                "skills": ["builtin:text_statistics@1"],
            },
        ).json()
        tid = client.post(
            "/api/v1/threads", headers=headers, json={"agent_id": agent["agent_id"]}
        ).json()["thread_id"]
        rid = client.post(
            "/api/v1/runs", headers=headers, json={"thread_id": tid, "prompt": "hello"}
        ).json()["run_id"]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    None, calls=[tool_call(name="text_stats", arguments='{"text":"hello"}')]
                ),
            )
        return httpx.Response(200, json=completion("5 个字符"))

    Worker(
        repository,
        harness_factory=lambda s: configured_harness(
            gateway.for_snapshot(s.model), transport=httpx.MockTransport(respond)
        ),
    ).run_once()
    assert repository.get(scope, rid).result == "5 个字符"
    assert repository.list_tool_calls(scope, rid)[0]["output"]["characters"] == 5
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "text_stats",
        "skill_read",
    ]


def test_concurrent_installs_have_one_identity(repository, scope):
    store = SkillRepository(repository.engine, repository.clock)
    doc = recipe()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: store.install(scope, "hub:" + "a" * 64, doc, doc.digest()), range(4))
        )
    assert len({item.reference for item in results}) == 1
    assert len(store.list(scope, 0, 10)) == 1


def test_install_requests_cannot_supply_urls_credentials_or_code(skill_client, hub, headers):
    for extra in (
        {"url": "https://other.example"},
        {"tenant_id": "other"},
        {"content": "x"},
        {"token": "secret"},
    ):
        assert (
            skill_client.post(
                "/api/v1/skills/install", json={**install_body(hub), **extra}, headers=headers
            ).status_code
            == 422
        )
    assert hub.requests == []
    assert skill_client.post("/api/v1/skills/install", json=install_body(hub)).status_code == 401
    assert hub.requests == []


def test_snapshot_budget_and_integrity_are_checked():
    first = BUILTIN_SKILLS[0].model_dump(mode="json")
    first["manifest"]["content"] = "tampered"
    with pytest.raises(ValidationError):
        ExecutionSnapshot(skills=[first])
    snapshots = []
    for index in range(3):
        doc = recipe(skill_id=f"skill_{index}", content="长" * 12000)
        snapshots.append(
            {
                "reference": f"builtin:{doc.skill_id}@1",
                "source": "builtin",
                "source_id": "builtin",
                "manifest": doc,
                "sha256": doc.digest(),
            }
        )
    with pytest.raises(ValidationError):
        ExecutionSnapshot(skills=snapshots)


def test_http_hub_contract_and_credentials_remain_on_fixed_origin():
    seen = []
    doc = recipe()

    def respond(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer hub-test-only"
        if request.url.path.endswith("/versions/1"):
            return httpx.Response(200, json=doc.model_dump(mode="json"))
        return httpx.Response(200, json={"items": [summary(doc)], "next_offset": None})

    hub = HttpSkillHub(
        SkillHubSettings("https://skills.example/v1", "hub-test-only"),
        transport=httpx.MockTransport(respond),
    )
    try:
        assert hub.catalog(0, 20, "摘要").items[0].sha256 == doc.digest()
        assert hub.manifest(doc.skill_id, 1) == doc
        assert seen[0].url.path == "/v1/skills" and seen[0].url.params["q"] == "摘要"
        assert seen[1].url.path == "/v1/skills/research_summary/versions/1"
        assert "hub-test-only" not in repr(hub.settings) + hub.source_id
        with pytest.raises(ValidationError):
            hub.manifest("../secret", 1)
        assert len(seen) == 2
    finally:
        hub.close()


@pytest.mark.parametrize(
    "case", ["redirect", "timeout", "bad_json", "large", "identity", "commands", "bad_page"]
)
def test_hub_failures_are_bounded_and_sanitized(case, settings, repository, headers):
    calls = []

    def respond(request):
        calls.append(request)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://unexpected.example/secret"})
        if case == "timeout":
            raise httpx.ReadTimeout("sensitive transport detail")
        if case == "bad_json":
            return httpx.Response(200, text="sensitive invalid response")
        if case == "large":
            return httpx.Response(200, content=b"x" * 512001)
        if case == "bad_page":
            return httpx.Response(200, json={"items": [], "next_offset": 99})
        document = recipe(
            skill_id="other" if case == "identity" else "research_summary"
        ).model_dump(mode="json")
        if case == "commands":
            document["install_command"] = "do not execute"
        return httpx.Response(200, json=document)

    hub = HttpSkillHub(
        SkillHubSettings("https://skills.example", "private-test-token"),
        transport=httpx.MockTransport(respond),
    )
    try:
        with TestClient(
            create_app(settings, repository, model_settings=ModelSettings(), skill_hub=hub)
        ) as client:
            path = (
                "/api/v1/skills/hub"
                if case == "bad_page"
                else "/api/v1/skills/hub/research_summary/versions/1"
            )
            response = client.get(path, headers=headers)
        assert response.status_code == (503 if case in {"redirect", "timeout"} else 502)
        assert not any(
            word in response.text for word in ("sensitive", "private-test-token", "skills.example")
        )
        assert len(calls) == 1
    finally:
        hub.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/skills",
        "https://user:secret@skills.example",
        "https://skills.example?q=token",
        "https://skills.example#fragment",
    ],
)
def test_hub_configuration_rejects_unsafe_url_forms(url):
    with pytest.raises(ValueError):
        SkillHubSettings(url)


def test_skill_read_cannot_read_unselected_recipe(repository, scope):
    from agentforge_runtime.errors import RuntimeFailure
    from agentforge_runtime.skill_tools import with_skill_reader
    from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
    from agentforge_worker.tools import RunToolJournal

    selected = BUILTIN_SKILLS[0]
    run = repository.create(
        scope, "read", execution_config=ExecutionSnapshot(tools=(), skills=(selected,))
    )
    claim = repository.claim_next()
    executor = ToolExecutor(
        ("skill_read@1",),
        registry=with_skill_reader(BUILTIN_TOOLS, (selected,)),
        allowed=("skill_read@1",),
        journal=RunToolJournal(repository, claim),
    )
    with pytest.raises(RuntimeFailure) as error:
        executor.prepare(
            "bad", "skill_read", json.dumps({"reference": BUILTIN_SKILLS[1].reference})
        )
    assert error.value.code == "tool_invalid_arguments"
    assert repository.list_tool_calls(scope, run.run_id) == []
    content = json.loads(
        executor.execute(
            executor.prepare("good", "skill_read", json.dumps({"reference": selected.reference}))
        )
    )
    assert content["content"].startswith("---\nname:")
    assert selected.manifest.content in content["content"]


def test_mock_skill_only_agent_reads_recipe_without_calling_model(
    client, headers, repository, scope
):
    agent = client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "摘要",
            "tools": [],
            "skills": ["builtin:document_summary@1"],
        },
    ).json()
    tid = client.post(
        "/api/v1/threads", headers=headers, json={"agent_id": agent["agent_id"]}
    ).json()["thread_id"]
    rid = client.post(
        "/api/v1/runs", headers=headers, json={"thread_id": tid, "prompt": "test"}
    ).json()["run_id"]
    Worker(repository).run_once()
    assert repository.get(scope, rid).status == "succeeded"
    assert [c["name"] for c in repository.list_tool_calls(scope, rid)] == ["skill_read"]
