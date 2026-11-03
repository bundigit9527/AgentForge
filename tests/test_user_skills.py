from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from agentforge_api.app import create_app
from agentforge_runtime.config import ModelSettings
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import user_skill_versions, user_skills
from agentforge_worker.worker import Worker
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests.test_identity import enterprise as _enterprise
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc
from tests.test_rpc_skills import catalog, send

enterprise = _enterprise
people = _people


def create(client, headers, **fields):
    result = rpc(
        client,
        headers,
        "agentSkills.create",
        {
            "name": "Research checklist",
            "description": "Check supplied evidence",
            "body": "Original steps",
            **fields,
        },
    )
    assert result.status_code == 200, result.text
    return result.json()["json"]


def versions(engine, skill_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                sa.select(user_skill_versions)
                .where(
                    user_skill_versions.c.skill_id == skill_id,
                )
                .order_by(user_skill_versions.c.version)
            )
            .mappings()
            .all()
        )


def test_native_skill_crud_preserves_stable_id_and_revisions(client, headers, engine, clock):
    item = create(client, headers)
    assert item["source"] == "user" and item["readOnly"] is False
    assert item["id"] in {s["id"] for s in catalog(client, headers)}
    assert (
        rpc(client, headers, "agentSkills.get", {"name": item["name"].upper()}).json()["json"]
        == item
    )
    clock.advance(2)
    changed = rpc(
        client,
        headers,
        "agentSkills.update",
        {
            "skillId": item["id"],
            "name": "Renamed checklist",
            "body": "Second steps",
        },
    )
    assert changed.status_code == 200, changed.text
    current = changed.json()["json"]
    assert current["id"] == item["id"] and current["createdAt"] == item["createdAt"]
    assert current["updatedAt"] != item["updatedAt"]
    assert "Second steps" in current["content"]
    assert [v["version"] for v in versions(engine, item["id"])] == [1, 2]
    assert "Original steps" in versions(engine, item["id"])[0]["manifest"]["content"]
    repeated = rpc(
        client,
        headers,
        "agentSkills.update",
        {"skillId": item["id"], "content": current["content"]},
    )
    assert repeated.json() == changed.json()
    assert len(versions(engine, item["id"])) == 2
    assert (
        rpc(
            client,
            headers,
            "agentSkills.update",
            {
                "skillId": item["id"],
                "body": "stale",
                "expectedVersion": 1,
            },
        ).status_code
        == 409
    )
    assert rpc(client, headers, "agentSkills.remove", {"skillId": item["id"]}).json()["json"] == {
        "ok": True
    }
    assert rpc(client, headers, "agentSkills.get", {"skillId": item["id"]}).status_code == 404
    assert (
        rpc(
            client, headers, "agentSkills.update", {"skillId": item["id"], "body": "revive"}
        ).status_code
        == 404
    )
    assert len(versions(engine, item["id"])) == 2
    assert create(client, headers, name="Renamed checklist")["id"] != item["id"]


def test_frontmatter_import_and_structured_updates_preserve_metadata(client, headers):
    content = """---
name: Analysis
description: >-
  Compare facts
  and identify gaps
license: MIT
metadata:
  owner: research
  labels: [summary, review]
allowed-tools: [shell]
---

  Indented body is retained.
"""
    item = create(client, headers, content=content)
    changed = rpc(
        client, headers, "agentSkills.update", {"skillId": item["id"], "description": "New summary"}
    )
    assert changed.status_code == 200
    text = changed.json()["json"]["content"]
    metadata = yaml.safe_load(text.split("---\n", 2)[1])
    assert metadata["name"] == "Analysis" and metadata["description"] == "New summary"
    assert metadata["metadata"] == {"owner": "research", "labels": ["summary", "review"]}
    assert metadata["allowed-tools"] == ["shell"] and metadata["license"] == "MIT"
    assert "  Indented body is retained." in text


@pytest.mark.parametrize(
    "content",
    [
        "missing frontmatter",
        "---\nname: private-secret\nname: duplicate\ndescription: x\n---\nbody",
        "---\nname: private-secret\ndescription: x\n"
        "metadata: !!python/object/apply:os.system [id]\n---\nbody",
        "---\nname: private-secret\ndescription: x\na: &root [*root]\n---\nbody",
        "---\nname: private-secret\ndescription: x\nmetadata: "
        + "[" * 20
        + "x"
        + "]" * 20
        + "\n---\nbody",
        "---\nname: private-secret\ndescription: x\nscore: .nan\n---\nbody",
        "---\nname: private-secret\ndescription: x\nmetadata: !!binary YWJj\n---\nbody",
        "---\nname: [not, scalar]\ndescription: x\n---\nbody",
        "---\nname: private-secret\ndescription: x\nrequired_tools: [unversioned-tool]\n---\nbody",
        "---\nname: private-secret\ndescription: x\n---\n" + "x" * 12000,
    ],
)
def test_invalid_yaml_is_rejected_without_echoing_document(client, headers, content):
    response = rpc(client, headers, "agentSkills.create", {"content": content})
    assert response.status_code == 422
    assert "private-secret" not in response.text
    assert len(catalog(client, headers)) == 3


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"content": None},
        {"name": "only name"},
        {"name": " ", "description": "x"},
        {"name": "x\ny", "description": "x"},
        {"name": "/hidden", "description": "x"},
        {"name": "Settings: General", "description": "x"},
        {"name": "x [v1:12345678]", "description": "x"},
        {"name": "x", "description": "x", "user_id": "forged"},
    ],
)
def test_invalid_structured_inputs_do_not_create_assets(client, headers, fields):
    assert rpc(client, headers, "agentSkills.create", fields).status_code == 422
    assert len(catalog(client, headers)) == 3


def test_duplicate_and_reserved_names_are_rejected_atomically(client, headers, engine):
    first = create(client, headers)
    assert (
        rpc(
            client,
            headers,
            "agentSkills.create",
            {"name": first["name"].upper(), "description": "x"},
        ).status_code
        == 409
    )
    assert (
        rpc(
            client, headers, "agentSkills.create", {"name": "资料摘要", "description": "x"}
        ).status_code
        == 409
    )
    other = create(client, headers, name="Different")
    assert (
        rpc(
            client, headers, "agentSkills.update", {"skillId": other["id"], "name": first["name"]}
        ).status_code
        == 409
    )
    assert len(versions(engine, other["id"])) == 1
    assert (
        rpc(client, headers, "agentSkills.get", {"skillId": other["id"]}).json()["json"]["name"]
        == "Different"
    )


def test_concurrent_create_and_partial_update(client, headers, engine):
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(
            pool.map(
                lambda name: rpc(
                    client,
                    headers,
                    "agentSkills.create",
                    {"name": name, "description": "x", "body": "initial"},
                ),
                ["Concurrent", "CONCURRENT"],
            )
        )
    assert sorted(result.status_code for result in created) == [200, 409]
    item = next(result.json()["json"] for result in created if result.status_code == 200)
    with ThreadPoolExecutor(max_workers=2) as pool:
        updated = list(
            pool.map(
                lambda fields: rpc(
                    client, headers, "agentSkills.update", {"skillId": item["id"], **fields}
                ),
                [{"description": "new description"}, {"body": "new body"}],
            )
        )
    assert [result.status_code for result in updated] == [200, 200]
    final = rpc(client, headers, "agentSkills.get", {"skillId": item["id"]}).json()["json"]
    assert final["description"] == "new description" and "new body" in final["content"]
    assert [row["version"] for row in versions(engine, item["id"])] == [1, 2, 3]


def test_private_user_skill_crud_authorization(enterprise):
    client, headers, _, _, hub = enterprise
    item = create(client, headers["alice"])
    for user in ("admin", "bob", "viewer"):
        assert item["id"] not in {s["id"] for s in catalog(client, headers[user])}
        for procedure, body in (
            ("agentSkills.get", {"skillId": item["id"]}),
            ("agentSkills.update", {"skillId": item["id"], "body": "steal"}),
            ("agentSkills.remove", {"skillId": item["id"]}),
        ):
            assert rpc(client, headers[user], procedure, body).status_code == 404
    assert (
        rpc(
            client, headers["viewer"], "agentSkills.create", {"name": "blocked", "description": "x"}
        ).status_code
        == 403
    )
    assert (
        rpc(
            client,
            {"Authorization": "Bearer invalid"},
            "agentSkills.create",
            {"name": "blocked", "description": "x"},
        ).status_code
        == 401
    )
    # Other members may independently use the same name without seeing this owner's recipe.
    assert create(client, headers["bob"])["id"] != item["id"]


@pytest.mark.parametrize("change", [{"tenant_id": "other"}, {"space_id": "other"}])
def test_user_skill_versions_are_scoped(client, headers, settings, repository, change):
    item = create(client, headers)
    reference = next(
        s["reference"]
        for s in client.get("/api/v1/skills", headers=headers).json()["items"]
        if s["source"] == "user"
    )
    with TestClient(
        create_app(
            replace(settings, dev_scope=replace(settings.dev_scope, **change)),
            repository,
            model_settings=ModelSettings(),
        )
    ) as other:
        assert rpc(other, headers, "agentSkills.get", {"skillId": item["id"]}).status_code == 404
        assert (
            other.post(
                "/api/v1/agents", headers=headers, json={"name": "steal", "skills": [reference]}
            ).status_code
            == 404
        )


def test_run_keeps_old_skill_across_edit_delete_and_recovery(
    client, headers, repository, scope, clock
):
    item = create(client, headers)
    bot = create_bot(client, headers)
    text = "/" + item["name"] + "\nprovided facts"
    receipt = send(client, headers, bot["id"], text)
    assert receipt.status_code == 200
    rid = receipt.json()["json"]["runId"]
    original = repository.get(scope, rid).execution_config
    assert original.skills[0].source == "user"
    assert original.skills[0].manifest.version == 1
    assert original.skills[0].reference != item["id"]
    assert repository.claim_next(lease_seconds=1)
    assert (
        rpc(
            client,
            headers,
            "agentSkills.update",
            {"skillId": item["id"], "body": "Newest instructions"},
        ).status_code
        == 200
    )
    assert rpc(client, headers, "agentSkills.remove", {"skillId": item["id"]}).status_code == 200
    assert send(client, headers, bot["id"], text).json() == receipt.json()
    clock.advance(2)
    assert Worker(repository).run_once()
    run = repository.get(scope, rid)
    assert run.status.value == "succeeded" and run.execution_config == original
    content = repository.list_tool_calls(scope, rid)[0]["output"]["content"]
    assert "Original steps" in content and "Newest instructions" not in content
    assert send(client, headers, bot["id"], text, "fresh").status_code == 404


def test_next_run_uses_updated_user_skill_but_prior_snapshot_is_unchanged(
    client, headers, repository, scope
):
    item = create(client, headers)
    bot = create_bot(client, headers)
    first = send(client, headers, bot["id"], "/" + item["name"]).json()["json"]["runId"]
    assert Worker(repository).run_once()
    assert (
        rpc(
            client, headers, "agentSkills.update", {"skillId": item["id"], "body": "Version two"}
        ).status_code
        == 200
    )
    second = send(client, headers, bot["id"], "/" + item["name"], "second").json()["json"]["runId"]
    assert repository.get(scope, first).execution_config.skills[0].manifest.version == 1
    assert repository.get(scope, second).execution_config.skills[0].manifest.version == 2


def test_user_skill_metadata_never_grants_tools(client, headers, repository, scope):
    item = create(
        client,
        headers,
        content=(
            "---\nname: Tool declaration\ndescription: x\n"
            "required_tools: [text_stats@1]\nallowed-tools: [shell]\n---\n"
            "Use only authorized tools"
        ),
    )
    bot = create_bot(client, headers)
    assert send(client, headers, bot["id"], "/" + item["name"]).status_code == 422
    agent = client.post(
        "/api/v1/agents", headers=headers, json={"name": "Stats", "tools": ["text_stats@1"]}
    ).json()
    receipt = send(client, headers, agent["agent_id"], "/" + item["name"])
    assert receipt.status_code == 200
    config = repository.get(scope, receipt.json()["json"]["runId"]).execution_config
    assert config.tools == ("text_stats@1",) and "shell" not in config.tools


def test_member_revocation_stops_user_skill_execution(enterprise, repository, clock):
    client, headers, identities, scopes, _ = enterprise
    item = create(client, headers["alice"])
    bot = create_bot(client, headers["alice"])
    rid = send(client, headers["alice"], bot["id"], "/" + item["name"]).json()["json"]["runId"]
    identities.set_member(
        scopes["admin"], "alice", display_name="alice", role="member", active=False
    )
    store = RunRepository(repository.engine, clock, auth_mode="database")
    Worker(store).run_once()
    with repository.engine.connect() as conn:
        from agentforge_storage.schema import runs, tool_calls

        assert (
            conn.execute(sa.select(runs.c.status).where(runs.c.run_id == rid)).scalar_one()
            != "succeeded"
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(tool_calls).where(tool_calls.c.run_id == rid)
            ).scalar_one()
            == 0
        )


def test_user_skill_migration_preserves_existing_data_and_blocks_loss(
    client, headers, repository, scope, engine
):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    old = repository.create(scope, "existing task")
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0009")
    assert client.get("/health/ready").status_code == 503
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    assert repository.get(scope, old.run_id).prompt == "existing task"
    create(client, headers)
    # Exercise the guard at its own revision; later migrations may independently
    # downgrade first, and SQLite does not roll back all DDL in a multi-step chain.
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0010")
    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0009")
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"
        )
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(user_skills)).scalar_one() == 1


def test_distinct_internal_spaces_remain_selectable(client, headers, repository, scope):
    first = create(client, headers, name="Team notes")
    second = create(client, headers, name="Team  notes")
    names = {entry["name"] for entry in catalog(client, headers)}
    assert first["name"] in names and second["name"] in names
    bot = create_bot(client, headers)
    response = send(client, headers, bot["id"], "/" + second["name"])
    assert response.status_code == 200
    run = repository.get(scope, response.json()["json"]["runId"])
    assert run.execution_config.skills[0].source_id == "user:" + second["id"]


def test_rest_binding_uses_revision_ids_and_keeps_pinned_content(
    client, headers, repository, scope
):
    item = create(client, headers)
    entry = next(
        s
        for s in client.get("/api/v1/skills", headers=headers).json()["items"]
        if s["source"] == "user"
    )
    assert entry["reference"] != item["id"]
    agent = client.post(
        "/api/v1/agents", headers=headers, json={"name": "Pinned", "skills": [entry["reference"]]}
    )
    assert agent.status_code == 201
    assert (
        client.post(
            "/api/v1/agents", headers=headers, json={"name": "Wrong ID", "skills": [item["id"]]}
        ).status_code
        == 404
    )
    assert (
        rpc(
            client,
            headers,
            "agentSkills.update",
            {"skillId": item["id"], "body": "Current content"},
        ).status_code
        == 200
    )
    thread = client.post(
        "/api/v1/threads", headers=headers, json={"agent_id": agent.json()["agent_id"]}
    ).json()
    assert rpc(client, headers, "agentSkills.remove", {"skillId": item["id"]}).status_code == 200
    run = client.post(
        "/api/v1/runs",
        headers=headers,
        json={"thread_id": thread["thread_id"], "prompt": "Use pinned recipe"},
    )
    assert run.status_code == 201
    assert Worker(repository).run_once()
    saved = repository.get(scope, run.json()["run_id"])
    assert saved.status.value == "succeeded"
    assert saved.execution_config.skills[0].manifest.version == 1
    assert "Original steps" in saved.execution_config.skills[0].manifest.content
    assert (
        client.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": "Removed", "skills": [entry["reference"]]},
        ).status_code
        == 404
    )


def test_delete_racing_update_never_reactivates_skill(client, headers, engine):
    item = create(client, headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(
            rpc,
            client,
            headers,
            "agentSkills.update",
            {"skillId": item["id"], "body": "Concurrent update"},
        )
        remove = pool.submit(rpc, client, headers, "agentSkills.remove", {"skillId": item["id"]})
        assert update.result().status_code in (200, 404)
        assert remove.result().status_code == 200
    assert rpc(client, headers, "agentSkills.get", {"skillId": item["id"]}).status_code == 404
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(user_skills.c.enabled).where(user_skills.c.skill_id == item["id"])
            ).scalar_one()
            is False
        )
