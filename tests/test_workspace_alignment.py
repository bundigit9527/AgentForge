import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from agentforge_core.attachments import artifact_name_key
from agentforge_core.models import RunNotFound, RunStatus, Scope
from agentforge_core.python_execution import EncodedFile, GeneratedFile, InputFile
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_runtime.workspace_tools import with_workspace_tools
from agentforge_storage.artifacts import ArtifactRepository, store_file
from agentforge_storage.conversations import ConversationRepository
from agentforge_storage.schema import chat_artifacts
from agentforge_supervisor.workspace_files import WorkspaceFileError, operate
from agentforge_worker.tools import RunToolJournal
from alembic import command
from alembic.config import Config

from tests.test_orpc import create_bot, rpc
from tests.test_workspace_bridge import claim_tool, execute, submit
from tests.test_workspace_file_tools import (
    answer,
    scripted_worker,
    write_args,
)
from tests.test_workspace_file_tools import browser_env as _browser_env
from tests.test_workspace_file_tools import people as _people
from tests.test_workspace_file_tools import workspace_env as _workspace_env

browser_env = _browser_env
people = _people
workspace_env = _workspace_env


def publisher(repository, claim, payload, *, before_publish=lambda: None):
    def request(tool, args):
        before_publish()
        return dict(name=args["path"].rsplit("/", 1)[-1], data=base64.b64encode(payload).decode())

    registry = with_workspace_tools(BUILTIN_TOOLS, request, "team", editing=True, aligned=True)
    return ToolExecutor(
        ("attach_file@3",),
        registry=registry,
        allowed=("attach_file@3",),
        read_allowed=("attach_file@3",),
        journal=RunToolJournal(repository, claim),
        checkpoint=lambda: repository.owns_claim(claim),
    )


def publish(executor, name, call_id="publish", path="report.txt", **extra):
    return json.loads(
        executor.execute(
            executor.prepare(
                call_id,
                "attach_file",
                json.dumps(dict(path=path, name=name, **extra)),
            )
        )
    )


@pytest.mark.parametrize("mode", ["team", "dedicated"])
def test_approved_nested_write_then_html_attachment(workspace_env, repository, mode):
    client, headers, bot, who, _, folders, calls, _ = workspace_env
    assert rpc(client, headers, "bots.setComputer", dict(botId=bot["id"], mode=mode)).is_success
    rid = submit(workspace_env)
    args = {**write_args("<h1>业务方案</h1>", mode=mode), "path": "reports/2026/index.html"}
    worker, _ = scripted_worker(
        repository,
        [
            ("write_file", args),
            ("attach_file", dict(path=args["path"], name="  业务方案  ", description="  网页稿  ")),
        ],
    )
    worker.run_once()
    assert folders == {} and calls == []
    answer(workspace_env, repository, rid)
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    assert calls[0]["action"] == "writeTextV2"
    folder = next(iter(folders.values()))
    target = folder / bot["id"] / args["path"] if mode == "team" else folder / args["path"]
    assert target.read_text() == args["content"]
    files = ArtifactRepository(repository)
    item = files.listing(who, bot["id"])[0]
    assert item["name"] == "业务方案" and item["description"] == "网页稿"
    assert item["mimeType"] == "text/html"
    assert base64.b64decode(files.get(who, item["id"])["contentBase64"]) == args["content"].encode()


def test_new_mkdir_does_not_change_legacy_or_override_missing_file(tmp_path):
    args = {**write_args("text"), "path": "new/deep/file.txt"}
    with pytest.raises(WorkspaceFileError, match="workspace_file_missing"):
        operate("writeText", args, tmp_path)
    assert not (tmp_path / "new").exists()
    with pytest.raises(WorkspaceFileError, match="workspace_file_missing"):
        operate("writeTextV2", {**args, "expected_sha256": "a" * 64}, tmp_path)
    assert not (tmp_path / "new").exists()
    for content in ("a" * 12001, "\0"):
        with pytest.raises(WorkspaceFileError):
            operate("writeTextV2", {**args, "content": content}, tmp_path)
        assert not (tmp_path / "new").exists()
    operate("writeTextV2", args, tmp_path)
    assert (tmp_path / args["path"]).read_text() == "text"


def test_name_matching_spans_history_but_cannot_read_other_versions(workspace_env, repository):
    client, headers, bot, who, *_ = workspace_env
    rid = submit(workspace_env)
    claim = repository.claim_next()
    ex = publisher(repository, claim, b"original")
    first = publish(ex, "  ÉTUDE 周报  ", description="  第一版  ")
    # More than the frozen catalogue's 12 entries; no catalogue search is used for versioning.
    for index in range(15):
        publish(ex, f"other-{index}", str(index))
    repository.finish(claim, result="done")
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    assert rpc(client, headers, "threads.clear", dict(botId=bot["id"])).is_success
    submit(workspace_env)
    later = repository.claim_next()
    assert not later.run.execution_config.python_files
    with pytest.raises(RunNotFound):
        repository.python_inputs(
            later, [InputFile(artifact_id=first["artifact_id"], name="old.txt")]
        )
    revised = publish(publisher(repository, later, b"revision"), "étude 周报")
    files = ArtifactRepository(repository)
    versions = files.versions(who, revised["artifact_id"])
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["id"] == first["artifact_id"]
    assert base64.b64decode(files.get(who, first["artifact_id"])["contentBase64"]) == b"original"
    # A version link grants no additional read permission to the old bytes.
    with pytest.raises(RunNotFound):
        repository.python_inputs(
            later, [InputFile(artifact_id=first["artifact_id"], name="old.txt")]
        )
    assert repository.python_inputs(
        later, [InputFile(artifact_id=revised["artifact_id"], name="new.txt")]
    )


def test_blank_and_omitted_titles_version_basename_and_replay_is_idempotent(
    workspace_env, repository
):
    submit(workspace_env)
    claim = repository.claim_next()
    ex = publisher(repository, claim, b"one")
    first = publish(ex, None)
    second = publish(ex, "  ", "second", description="  ")
    assert publish(ex, "  ", "second", description="  ") == second
    assert first["name"] == second["name"] == "report.txt"
    files = ArtifactRepository(repository)
    assert [v["version"] for v in files.versions(workspace_env[3], first["artifact_id"])] == [2, 1]
    long_title = "方案" * 150
    titled = publish(ex, long_title, "long", description="摘" * 300)
    item = files.get(workspace_env[3], titled["artifact_id"])
    assert len(item["name"]) == 255 and len(item["description"]) == 280


@pytest.mark.parametrize("boundary", ["tenant", "space", "user", "bot"])
def test_same_name_does_not_cross_owner_or_bot(workspace_env, repository, boundary):
    client, headers, bot, who, *_ = workspace_env
    other = create_bot(client, headers)["id"]
    scopes = {
        "tenant": Scope("other-tenant", who.space_id, who.user_id),
        "space": Scope(who.tenant_id, "other-space", who.user_id),
        "user": Scope(who.tenant_id, who.space_id, "bob"),
        "bot": who,
    }
    with repository.engine.begin() as conn:
        old, _ = store_file(
            conn,
            scopes[boundary],
            other if boundary == "bot" else bot["id"],
            None,
            "REPORT.TXT",
            "text/plain",
            None,
            b"private",
            None,
            repository.clock(),
        )
    submit(workspace_env)
    claim = repository.claim_next()
    result = publish(publisher(repository, claim, b"mine"), None)
    versions = ArtifactRepository(repository).versions(who, result["artifact_id"])
    assert len(versions) == 1 and versions[0]["id"] != old["artifact_id"]


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_same_name_publications_share_one_family(workspace_env, repository, existing):
    _, _, bot, who, *_ = workspace_env
    submit(workspace_env)
    first = repository.claim_next()
    if existing:
        publish(publisher(repository, first, b"base"), "REPORT.TXT", "base")
    thread = ConversationRepository(repository.engine, repository.clock).create_thread(
        who, bot["id"], "Parallel"
    )
    repository.create(
        who,
        "parallel publication",
        thread_id=thread["thread_id"],
        _resolve_config=lambda conn, config: first.run.execution_config,
    )
    second = repository.claim_next()
    barrier = Barrier(2, timeout=10)

    def attach(claim):
        ex = publisher(repository, claim, claim.run.run_id.encode(), before_publish=barrier.wait)
        return publish(ex, "Report.txt" if claim == first else "report.TXT")["artifact_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(attach, [first, second]))
    files = ArtifactRepository(repository)
    versions = files.versions(who, ids[0])
    assert [v["version"] for v in versions] == ([3, 2, 1] if existing else [2, 1])
    assert {v["id"] for v in versions[:2]} == set(ids)
    assert len(files.listing(who, bot["id"], limit=20)["items"]) == 1


@pytest.mark.parametrize(
    "suffix,mime",
    [
        ("html", "text/html"),
        ("htm", "text/html"),
        ("markdown", "text/markdown"),
        ("gif", "image/gif"),
        ("webp", "image/webp"),
        ("jpeg", "image/jpeg"),
    ],
)
def test_extended_types_pass_api_and_publication_but_legacy_decode_stays_restricted(
    workspace_env,
    repository,
    suffix,
    mime,
):
    client, _, bot, who, _, folders, *_ = workspace_env
    claim, args = claim_tool(workspace_env, repository)
    assert execute(client, claim, "list_files", args).is_success
    data = b"<h1>document</h1>"
    if mime.startswith("image/"):
        images = json.loads((Path(__file__).parent / "fixtures/workspace-images.json").read_text())
        data = base64.b64decode(images[suffix])
    name = "export." + suffix.upper()
    target = next(iter(folders.values())) / bot["id"] / name
    target.write_bytes(data)
    repository.finish(claim, result="ready")
    rid = submit(workspace_env)
    worker, _ = scripted_worker(repository, [("attach_file", dict(path=name))])
    worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    file = next(
        i for i in ArtifactRepository(repository).listing(who, bot["id"]) if i["name"] == name
    )
    assert file["mimeType"] == mime
    with pytest.raises(ValueError):
        GeneratedFile.decode(EncodedFile(name=name, data=base64.b64encode(data).decode()))


def test_name_index_migration_preserves_files_and_versions(workspace_env, repository):
    who = workspace_env[3]
    submit(workspace_env)
    claim = repository.claim_next()
    ex = publisher(repository, claim, b"immutable")
    first = publish(ex, "ÉTUDE")
    publish(ex, "étude", "again")
    files = ArtifactRepository(repository)
    before = files.versions(who, first["artifact_id"])
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with repository.engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0018")
        command.upgrade(config, "head")
        keys = conn.execute(sa.select(chat_artifacts.c.name_key)).scalars().all()
        assert keys == [artifact_name_key("étude")] * 2
    assert files.versions(who, first["artifact_id"]) == before
    assert base64.b64decode(files.get(who, first["artifact_id"])["contentBase64"]) == b"immutable"


def test_failed_event_rolls_back_attachment_version_and_tool(
    workspace_env, repository, monkeypatch
):
    submit(workspace_env)
    claim = repository.claim_next()
    ex = publisher(repository, claim, b"value")
    first = publish(ex, "report")

    def fail(*args, **kwargs):
        raise RuntimeError("event storage unavailable")

    monkeypatch.setattr("agentforge_storage.artifacts.append", fail)
    with pytest.raises((RuntimeError, RuntimeFailure)):
        publish(ex, "REPORT", "failure")
    assert len(ArtifactRepository(repository).versions(workspace_env[3], first["artifact_id"])) == 1
    calls = repository.list_tool_calls(workspace_env[3], claim.run.run_id)
    assert not any(c["call_id"] == "failure" and c["status"] == "succeeded" for c in calls)
