import base64
import hashlib
import json

import httpx
import pytest
import sqlalchemy as sa
from agentforge_connectors.workspace import prepare_workspace
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunStatus
from agentforge_core.workspace import ReadFile, WriteFile, workspace_tools
from agentforge_runtime.chat_harness import configured_harness
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_storage.schema import approval_requests, external_effects, runs
from agentforge_supervisor.workspace_files import WorkspaceFileError, operate
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_orpc import rpc
from tests.test_workspace_bridge import (
    browser_env as _browser_env,
)
from tests.test_workspace_bridge import (
    claim_tool,
    execute,
    submit,
)
from tests.test_workspace_bridge import (
    people as _people,
)
from tests.test_workspace_bridge import (
    workspace_env as _workspace_env,
)

browser_env = _browser_env
people = _people
workspace_env = _workspace_env


def scripted_worker(repository, actions):
    requests = []

    def model(request):
        body = json.loads(request.content)
        index = len(requests)
        requests.append(body)
        if index == len(actions):
            return httpx.Response(200, json=completion("文件操作完成。"))
        name, arguments = actions[index]
        if callable(arguments):
            arguments = arguments(body)
        return httpx.Response(
            200,
            json=completion(None, [tool_call(str(index), name, json.dumps(arguments))]),
        )

    return Worker(
        repository,
        harness_factory=lambda _: configured_harness(
            model_settings(), transport=httpx.MockTransport(model)
        ),
    ), requests


def answer(env, repository, rid, decision="allow"):
    client, headers, bot, who, *_ = env
    with repository.engine.connect() as conn:
        card = (
            conn.execute(
                sa.select(approval_requests)
                .join(external_effects)
                .where(approval_requests.c.run_id == rid, external_effects.c.status == "pending")
            )
            .mappings()
            .one()
        )
    response = rpc(
        client,
        headers,
        "threads.answer",
        dict(botId=bot["id"], runId=rid, messageId=card["message_id"], answer=decision),
    )
    assert response.is_success, response.text
    return card


def write_args(content, *, digest=None, mode="team"):
    return dict(path="note.txt", content=content, expected_sha256=digest, computer_mode=mode)


@pytest.mark.parametrize("mode", ["team", "dedicated"])
def test_create_read_edit_and_named_versions_through_worker(workspace_env, repository, mode):
    client, headers, bot, who, _, folders, _, _ = workspace_env
    assert rpc(client, headers, "bots.setComputer", dict(botId=bot["id"], mode=mode)).is_success
    rid = submit(workspace_env)
    title = "项目进展 / 技术方案"
    worker, requests = scripted_worker(
        repository,
        [
            ("write_file", write_args("第一版\n", mode=mode)),
            ("attach_file", dict(path="note.txt", name=title, description="首版")),
            ("read_file", dict(path="note.txt")),
            (
                "write_file",
                lambda body: write_args(
                    "第二版\n",
                    digest=json.loads(body["messages"][-1]["content"])["sha256"],
                    mode=mode,
                ),
            ),
            ("attach_file", dict(path="note.txt", name=title, description="修订版")),
        ],
    )
    assert worker.run_once()
    assert repository.get(who, rid).status == RunStatus.WAITING_INPUT
    assert folders == {}
    card = answer(workspace_env, repository, rid)
    assert ("同空间成员可见" in card["blocks"][0]["text"]) == (mode == "team")
    assert worker.run_once()
    assert repository.get(who, rid).status == RunStatus.WAITING_INPUT
    folder = next(iter(folders.values()))
    target = folder / bot["id"] / "note.txt" if mode == "team" else folder / "note.txt"
    assert target.read_text() == "第一版\n"
    answer(workspace_env, repository, rid)
    assert worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    assert target.read_text() == "第二版\n"
    artifacts = ArtifactRepository(repository)
    listing = artifacts.listing(who, bot["id"], limit=20)["items"]
    assert len(listing) == 1 and listing[0]["versionCount"] == 2
    assert listing[0]["name"] == title and listing[0]["description"] == "修订版"
    versions = artifacts.versions(who, listing[0]["id"])
    assert [v["version"] for v in versions] == [2, 1]
    assert (
        base64.b64decode(artifacts.get(who, versions[1]["id"])["contentBase64"])
        == "第一版\n".encode()
    )
    assert len(requests) == 6
    # Subsequent turns get a safe local filename alias, not the display title as a path.
    rid2 = submit(workspace_env)
    config = repository.get(who, rid2).execution_config
    assert config.python_files[0].name.endswith(".txt") and "/" not in config.python_files[0].name
    worker2, _ = scripted_worker(repository, [("attach_file", dict(path="note.txt", name=title))])
    assert worker2.run_once()
    assert repository.get(who, rid2).status == RunStatus.SUCCEEDED
    assert artifacts.listing(who, bot["id"], limit=20)["items"][0]["versionCount"] == 3


def test_write_conflict_is_failed_and_model_can_read_current_file(workspace_env, repository):
    who = workspace_env[3]
    rid = submit(workspace_env)
    worker, requests = scripted_worker(
        repository,
        [
            ("write_file", write_args("first")),
            ("write_file", write_args("must not overwrite")),
            ("read_file", dict(path="note.txt")),
        ],
    )
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    assert "workspace_file_changed" in requests[2]["messages"][-1]["content"]
    assert json.loads(requests[3]["messages"][-1]["content"])["content"] == "first"
    calls = repository.list_tool_calls(who, rid)
    assert [c["status"] for c in calls] == ["succeeded", "failed", "succeeded"]
    with repository.engine.connect() as conn:
        statuses = (
            conn.execute(
                sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
            )
            .scalars()
            .all()
        )
    assert sorted(statuses) == ["failed", "succeeded"]


def test_approval_does_not_overwrite_intervening_manual_edit(workspace_env, repository):
    client, headers, bot, who, _, folders, _, _ = workspace_env
    rid = submit(workspace_env)
    old = hashlib.sha256(b"initial").hexdigest()
    worker, _ = scripted_worker(
        repository,
        [
            ("write_file", write_args("initial")),
            ("write_file", write_args("agent edit", digest=old)),
            ("read_file", dict(path="note.txt")),
        ],
    )
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    target = next(iter(folders.values())) / bot["id"] / "note.txt"
    target.write_text("manual edit")
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert target.read_text() == "manual edit"
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED


def legacy_editing(repository, who, rid):
    config = repository.get(who, rid).execution_config.model_dump()
    config.pop("workspace_file_alignment")
    config["approval_tools"] = ["workspace_save@1", "write_file@1"]
    with repository.engine.begin() as conn:
        conn.execute(runs.update().where(runs.c.run_id == rid).values(execution_config=config))


def test_legacy_named_versions_do_not_cross_clear_boundary_or_user(workspace_env, repository):
    client, headers, bot, who, *_ = workspace_env
    rid = submit(workspace_env)
    legacy_editing(repository, who, rid)
    worker, _ = scripted_worker(
        repository,
        [
            ("write_file", write_args("a")),
            ("attach_file", dict(path="note.txt", name="周报")),
            ("attach_file", dict(path="note.txt")),
            ("attach_file", dict(path="note.txt")),
            ("attach_file", dict(path="note.txt", name="另一份周报")),
        ],
    )
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    artifacts = ArtifactRepository(repository)
    assert len(artifacts.listing(who, bot["id"], limit=20)["items"]) == 4
    first = next(a for a in artifacts.listing(who, bot["id"]) if a["name"] == "周报")
    from agentforge_core.models import Scope

    assert artifacts.listing(Scope(who.tenant_id, who.space_id, "bob"), bot["id"]) == []
    assert rpc(client, headers, "threads.clear", dict(botId=bot["id"])).is_success
    rid2 = submit(workspace_env)
    legacy_editing(repository, who, rid2)
    assert not repository.get(who, rid2).execution_config.python_files
    worker2, _ = scripted_worker(repository, [("attach_file", dict(path="note.txt", name="周报"))])
    worker2.run_once()
    assert repository.get(who, rid2).status == RunStatus.SUCCEEDED
    assert len(artifacts.versions(who, first["id"])) == 1
    assert len(artifacts.listing(who, bot["id"], limit=20)["items"]) == 5


@pytest.mark.parametrize("decision", ["deny", "cancel"])
def test_write_denial_or_cancel_does_not_touch_disk(workspace_env, repository, decision):
    who, folders = workspace_env[3], workspace_env[5]
    rid = submit(workspace_env)
    worker, _ = scripted_worker(repository, [("write_file", write_args("never"))])
    worker.run_once()
    if decision == "deny":
        answer(workspace_env, repository, rid, "deny")
        worker.run_once()
        assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    else:
        repository.cancel(who, rid)
        assert not worker.run_once()
    assert folders == {}


def test_write_service_requires_exact_approved_dispatch(workspace_env, repository):
    claim, args = claim_tool(workspace_env, repository, "write_file", write_args("never"))
    assert execute(workspace_env[0], claim, "write_file", args).status_code == 403
    assert not workspace_env[6]


def test_old_snapshot_cannot_gain_file_editing(workspace_env, repository):
    claim, args = claim_tool(workspace_env, repository, "read_file", dict(path="note.txt"))
    config = claim.run.execution_config.model_dump()
    config.pop("workspace_file_tools")
    config.pop("workspace_file_alignment")
    config["approval_tools"] = ["workspace_save@1"]
    with repository.engine.begin() as conn:
        conn.execute(
            runs.update().where(runs.c.run_id == claim.run.run_id).values(execution_config=config)
        )
    assert execute(workspace_env[0], claim, "read_file", args).status_code == 403
    legacy = ExecutionSnapshot.model_validate(config)
    assert not legacy.workspace_file_tools and "attach_file@1" in legacy.tool_references
    assert "write_file@1" not in legacy.tool_references


def test_full_tool_budget_preserves_previous_workspace_subset(workspace_env):
    from tests.test_python_execution import IMAGE

    config = ExecutionSnapshot(
        model=model_settings().snapshot(),
        python_image=IMAGE,
        python_continuity=True,
        tools=tuple(f"tool_{i}@1" for i in range(12)),
    )
    updated = prepare_workspace(workspace_env[3], config)
    assert updated.workspace_enabled and not updated.workspace_file_tools
    assert set(workspace_tools()) <= set(updated.tool_references)
    assert len(updated.tool_references) == 16


def test_unicode_pages_and_conflict_detection(tmp_path):
    text = "中文😀\n" * 1300
    (tmp_path / "test.txt").write_text(text)
    args = dict(path="test.txt", offset=0, limit=2000)
    first = operate("readText", args, tmp_path)
    assert first["content"] == text[:2000] and first["truncated"]
    assert first["size"] == len(text.encode())
    all_text = first["content"]
    page = first
    while page["next_offset"] is not None:
        args.update(offset=page["next_offset"], expected_sha256=first["sha256"])
        page = operate("readText", args, tmp_path)
        all_text += page["content"]
    assert all_text == text
    (tmp_path / "test.txt").write_text("changed")
    with pytest.raises(WorkspaceFileError, match="workspace_file_changed"):
        operate("readText", args, tmp_path)


@pytest.mark.parametrize("contents", [b"\xff", b"a\0b", b"a" * (2 * 1024 * 1024 + 1)])
def test_non_text_and_oversized_files_are_not_read(tmp_path, contents):
    (tmp_path / "data.txt").write_bytes(contents)
    with pytest.raises(WorkspaceFileError, match="workspace_file_unsupported"):
        operate("readText", dict(path="data.txt"), tmp_path)


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "fifo", "directory", "escape"])
def test_file_tools_cannot_escape_or_overwrite_special_files(tmp_path, kind):
    import os

    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    path = "note.txt"
    if kind == "file_link":
        (root / path).symlink_to(secret)
    elif kind == "parent_link":
        (root / "parent").symlink_to(tmp_path)
        path = "parent/secret.txt"
    elif kind == "fifo":
        os.mkfifo(root / path)
    elif kind == "directory":
        (root / path).mkdir()
    else:
        path = "../secret.txt"
    for action in ("readText", "writeText", "writeTextV2"):
        with pytest.raises((ValueError, OSError)):
            operate(action, dict(path=path, content="bad", expected_sha256=None), root)
    assert secret.read_text() == "secret"


def test_arguments_are_bounded_and_overwrite_intent_is_required():
    with pytest.raises(ValueError):
        WriteFile(path="x.txt", content="x", computer_mode="team")
    with pytest.raises(ValueError):
        WriteFile(**write_args("\0"))
    with pytest.raises(ValueError):
        ReadFile(path="../escape", limit=2000)
    with pytest.raises(ValueError):
        ReadFile(path="x.txt", limit=2001)


def test_unknown_write_result_is_not_replayed(workspace_env, repository, monkeypatch):
    import agentforge_worker.worker as module
    from agentforge_core.models import RunErrorCode
    from agentforge_runtime.errors import RuntimeFailure

    original = module.workspace_request

    def lost_response(claim, tool, arguments):
        original(claim, tool, arguments)
        raise RuntimeFailure(RunErrorCode.TOOL_EXECUTION_FAILED)

    monkeypatch.setattr(module, "workspace_request", lost_response)
    who = workspace_env[3]
    rid = submit(workspace_env)
    worker, _ = scripted_worker(repository, [("write_file", write_args("written but unknown"))])
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.FAILED
    assert len(workspace_env[6]) == 1
    with repository.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
            ).scalar_one()
            == "dispatched"
        )
    assert not worker.run_once()
    assert len(workspace_env[6]) == 1


def test_cancellation_after_copy_does_not_claim_success(workspace_env, repository, monkeypatch):
    from agentforge_api.desktop import Desktop

    original = Desktop.exchange
    who = workspace_env[3]
    rid = submit(workspace_env)

    def cancel_after_write(self, action, *params):
        result = original(self, action, *params)
        if action == "workspace" and params[1]["action"] in {"writeText", "writeTextV2"}:
            repository.cancel(who, rid)
        return result

    monkeypatch.setattr(Desktop, "exchange", cancel_after_write)
    worker, _ = scripted_worker(repository, [("write_file", write_args("possibly written"))])
    worker.run_once()
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.CANCELLED
    target = next(iter(workspace_env[5].values())) / workspace_env[2]["id"] / "note.txt"
    assert target.read_text() == "possibly written"
    with repository.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
            ).scalar_one()
            == "dispatched"
        )
    assert not ArtifactRepository(repository).listing(who, workspace_env[2]["id"])


@pytest.mark.parametrize(
    "code",
    [
        "workspace_file_changed",
        "workspace_file_missing",
        "workspace_file_unsupported",
    ],
)
def test_transport_preserves_only_definite_file_refusals(
    workspace_env, repository, monkeypatch, code
):
    from agentforge_connectors.workspace import workspace_request
    from agentforge_runtime.errors import RuntimeFailure

    claim, args = claim_tool(workspace_env, repository, "read_file", dict(path="note.txt"))
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(lambda req: httpx.Response(409, json={"error": code})),
        ),
    )
    with pytest.raises(RuntimeFailure) as failure:
        workspace_request(claim, "read_file", args)
    assert failure.value.code.value == code
