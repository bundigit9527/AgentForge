import base64
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from agentforge_api.desktop import Desktop
from agentforge_core.models import RunErrorCode, RunStatus
from agentforge_core.workspace import ARGUMENTS
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_runtime.workspace_tools import with_workspace_tools
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_supervisor.workspace_files import WorkspaceFileError, operate
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_approval_resume import card_for
from tests.test_browser_auth import ORIGIN, login
from tests.test_identity import people as _people
from tests.test_orpc import rpc
from tests.test_python_execution import IMAGE
from tests.test_visible_browser import TOKEN
from tests.test_visible_browser import browser_env as _browser_env

people = _people
browser_env = _browser_env


@pytest.fixture
def workspace_env(browser_env, people, tmp_path, monkeypatch):
    client, headers, bot, _, _, _, _ = browser_env
    who = people[1]["alice"]
    policy = tmp_path / "workspace.json"
    policy.write_text(
        json.dumps(
            {"grants": [dict(tenant_id=who.tenant_id, space_id=who.space_id, users=[who.user_id])]}
        )
    )
    monkeypatch.setenv("AGENTFORGE_WORKSPACE_POLICY_FILE", str(policy))
    monkeypatch.setenv("AGENTFORGE_PYTHON_POLICY_FILE", str(policy))
    monkeypatch.setenv("AGENTFORGE_PYTHON_IMAGE", IMAGE)
    monkeypatch.setenv("AGENTFORGE_MCP_COMPUTER_TOKEN", "c" * 32)
    original = Desktop.exchange
    folders, calls, hooks = {}, [], []

    def exchange(self, action, *params):
        if action != "workspace":
            return original(self, action, *params)
        generation, body = params
        assert generation == self.generation
        calls.append(body)

        def check():
            return client.post(
                "/api/internal/workspace/check",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"operationId": body["operationId"]},
            )

        assert check().is_success
        if hooks:
            hooks.pop(0)(self, body)
        if not check().is_success:
            return {"error": "workspace_not_allowed"}
        key = tuple(self.owner)
        if key not in folders:
            folders[key] = tmp_path / ("desktop-" + str(len(folders)))
            folders[key].mkdir()
        try:
            return {"result": operate(body["action"], body["request"], folders[key])}
        except WorkspaceFileError as error:
            return {"error": error.code}

    monkeypatch.setattr(Desktop, "exchange", exchange)

    def request(claim, tool, args):
        response = client.post(
            "/api/internal/workspace/execute",
            headers={"Authorization": "Bearer " + TOKEN},
            json=dict(
                runId=claim.run.run_id, attemptId=claim.attempt_id, tool=tool, arguments=args
            ),
        )
        if response.status_code == 409 and response.json().get("error") in {
            "workspace_file_changed",
            "workspace_file_unsupported",
            "workspace_file_missing",
        }:
            raise RuntimeFailure(RunErrorCode(response.json()["error"]))
        assert response.is_success, response.text
        return response.json()

    monkeypatch.setattr("agentforge_worker.worker.workspace_request", request)
    yield client, headers, bot, who, policy, folders, calls, hooks


def upload(env):
    client, headers, bot, *_ = env
    response = rpc(
        client,
        headers,
        "artifacts.create",
        dict(
            botId=bot["id"],
            name="report.txt",
            mimeType="text/plain",
            contentBase64=base64.b64encode(b"source bytes").decode(),
        ),
    )
    assert response.is_success, response.text
    return response.json()["json"]["id"]


def submit(env, artifact=None):
    client, headers, bot, who, *_ = env
    response = rpc(
        client,
        headers,
        "threads.send",
        dict(
            botId=bot["id"],
            text="把附件保存到电脑，然后取回聊天",
            artifactIds=[artifact] if artifact else [],
        ),
    )
    assert response.is_success, response.text
    return response.json()["json"]["runId"]


@pytest.mark.parametrize("mode", ["team", "dedicated"])
def test_save_approval_then_attach_file_full_loop(workspace_env, repository, mode):
    client, headers, bot, who, _, folders, calls, _ = workspace_env
    assert rpc(client, headers, "bots.setComputer", dict(botId=bot["id"], mode=mode)).is_success
    ref = upload(workspace_env)
    rid = submit(workspace_env, ref)
    config = repository.get(who, rid).execution_config
    assert config.workspace_enabled and config.workspace_mode == mode
    phases = []
    path = f"artifacts/{ref}/report.txt"

    def model(request):
        body = json.loads(request.content)
        index = len(phases)
        phases.append(body)
        if index == 0:
            tool, args = "workspace_save", dict(artifact_id=ref, computer_mode=mode)
        elif index == 1:
            assert json.loads(body["messages"][-1]["content"])["path"] == path
            tool, args = "list_files", dict(path=f"artifacts/{ref}")
        elif index == 2:
            assert path in body["messages"][-1]["content"]
            tool, args = "attach_file", dict(path=path)
        else:
            receipt = json.loads(body["messages"][-1]["content"])
            assert receipt["artifact_id"] != ref
            assert "base64" not in body["messages"][-1]["content"]
            return httpx.Response(200, json=completion("已保存并取回，电脑源文件保持不变。"))
        return httpx.Response(
            200, json=completion(None, [tool_call(str(index), tool, json.dumps(args))])
        )

    gateway = model_settings()
    worker = Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    )
    assert worker.run_once()
    run = repository.get(who, rid)
    assert run.status == RunStatus.WAITING_INPUT and calls == []
    card = card_for(repository, run)
    assert ("同空间成员可见" in card["blocks"][0]["text"]) == (mode == "team")
    assert rpc(
        client,
        headers,
        "threads.answer",
        dict(botId=bot["id"], runId=rid, messageId=card["message_id"], answer="allow"),
    ).is_success
    assert worker.run_once()
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED
    assert len(phases) == 4 and len(calls) == 3
    items = ArtifactRepository(repository).listing(who, bot["id"])
    assert len(items) == 2
    assert all(
        base64.b64decode(ArtifactRepository(repository).get(who, item["id"])["contentBase64"])
        == b"source bytes"
        for item in items
    )
    folder = next(iter(folders.values()))
    target = folder / bot["id"] / path if mode == "team" else folder / path
    assert target.read_bytes() == b"source bytes"


def claim_tool(env, repository, tool="list_files", args=None):
    ref = upload(env)
    submit(env, ref)
    claim = repository.claim_next()
    args = args if args is not None else {"path": ""}
    registry = with_workspace_tools(
        BUILTIN_TOOLS,
        lambda *_: {},
        claim.run.execution_config.workspace_mode,
        editing=claim.run.execution_config.workspace_file_tools,
        aligned=claim.run.execution_config.workspace_file_alignment,
    )
    reference = next(
        r for r in claim.run.execution_config.tool_references if r.split("@")[0] == tool
    )
    executor = ToolExecutor(
        (reference,),
        registry=registry,
        allowed=(reference,),
        read_allowed=(reference,),
        write_allowed=("workspace_save@1", "write_file@1", "write_file@2"),
    )
    prepared = executor.prepare("first", tool, json.dumps(args))
    repository.start_tool(claim, prepared.invocation)
    return claim, prepared.arguments.model_dump()


def execute(client, claim, tool, args, **overrides):
    return client.post(
        "/api/internal/workspace/execute",
        headers={"Authorization": "Bearer " + TOKEN},
        json={
            "runId": claim.run.run_id,
            "attemptId": claim.attempt_id,
            "tool": tool,
            "arguments": args,
            **overrides,
        },
    )


def test_service_token_current_tool_and_approval_are_required(workspace_env, repository):
    client, _, _, _, _, _, calls, _ = workspace_env
    claim, args = claim_tool(workspace_env, repository)
    assert client.post("/api/internal/workspace/execute", json={}).status_code == 403
    assert execute(client, claim, "list_files", args, attemptId=str(uuid4())).status_code == 403
    assert execute(client, claim, "list_files", {"path": "other"}).status_code == 403
    assert (
        execute(
            client,
            claim,
            "workspace_save",
            {"artifact_id": claim.run.input_files[0]["artifactId"], "computer_mode": "team"},
        ).status_code
        == 403
    )
    assert calls == []


@pytest.mark.parametrize("tool", ["list_files", "read_file"])
@pytest.mark.parametrize("stop", ["policy", "lease", "cancel", "membership"])
def test_live_authorization_before_desktop(workspace_env, repository, clock, people, stop, tool):
    client, _, _, who, policy, _, calls, _ = workspace_env
    claim, args = claim_tool(
        workspace_env, repository, tool, {"path": "" if tool == "list_files" else "x.txt"}
    )
    if stop == "policy":
        policy.write_text('{"grants":[]}')
    elif stop == "lease":
        clock.advance(1000)
    elif stop == "cancel":
        repository.cancel(who, claim.run.run_id)
    else:
        people[0].set_member(
            people[1]["admin"], "alice", display_name="Alice", role="member", active=False
        )
    assert execute(client, claim, tool, args).status_code == 403
    assert calls == []


def test_takeover_blocks_agent_files_and_cancel_during_read(workspace_env, repository, people):
    client, headers, bot, who, _, _, calls, hooks = workspace_env
    assert login(client, people[2]["alice"]["token"]).is_success
    web = {"Origin": ORIGIN}

    def call(action):
        return rpc(client, web, "computer." + action, {"botId": bot["id"]})

    assert call("boot").is_success and call("takeover").is_success
    claim, args = claim_tool(workspace_env, repository)
    assert execute(client, claim, "list_files", args).status_code == 409 and calls == []
    assert call("release").is_success

    def stop(slot, body):
        assert call("takeover").status_code == 409
        repository.cancel(who, claim.run.run_id)

    hooks.append(stop)
    assert execute(client, claim, "list_files", args).status_code == 403


def artifact_request(data=b"contents"):
    ref = str(uuid4())
    return dict(
        path=f"artifacts/{ref}/report.txt",
        artifactId=ref,
        name="report.txt",
        contentBase64=base64.b64encode(data).decode(),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def test_atomic_desktop_copy_is_idempotent_and_never_overwrites(tmp_path):
    request = artifact_request()
    a = operate("saveArtifact", request, tmp_path)
    assert operate("saveArtifact", request, tmp_path) == a
    file = tmp_path / request["path"]
    assert file.read_bytes() == b"contents"
    file.write_bytes(b"desktop edit")
    with pytest.raises(ValueError):
        operate("saveArtifact", request, tmp_path)
    assert file.read_bytes() == b"desktop edit"
    assert list(file.parent.glob(".af-artifact-*")) == []


@pytest.mark.parametrize(
    "attack", ["parent_symlink", "target_symlink", "directory", "digest", "traversal"]
)
def test_desktop_copy_rejects_unsafe_targets(tmp_path, attack):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("unchanged")
    request = artifact_request()
    target = root / request["path"]
    if attack == "parent_symlink":
        (root / "artifacts").symlink_to(outside, target_is_directory=True)
    elif attack in ("target_symlink", "directory"):
        target.parent.mkdir(parents=True)
        if attack == "target_symlink":
            target.symlink_to(secret)
        else:
            target.mkdir()
    elif attack == "digest":
        request["sha256"] = "0" * 64
    else:
        request["path"] = "../outside/secret.txt"
    with pytest.raises((OSError, ValueError)):
        operate("saveArtifact", request, root)
    assert secret.read_text() == "unchanged"


def test_safe_relative_file_arguments():
    for name in ["/etc/passwd", "../x", "a/../b", "a\\b", "a//b"]:
        for model in (ARGUMENTS["list_files"], ARGUMENTS["attach_file"]):
            with pytest.raises(ValueError):
                model(path=name)


def test_running_save_without_approved_dispatch_cannot_copy(workspace_env, repository):
    client, _, _, _, _, _, calls, _ = workspace_env
    args = dict(artifact_id=str(uuid4()), computer_mode="team")
    claim, args = claim_tool(workspace_env, repository, "workspace_save", args)
    assert execute(client, claim, "workspace_save", args).status_code == 403
    assert calls == []


def test_reject_save_keeps_desktop_unchanged(workspace_env, repository):
    client, headers, bot, who, _, folders, calls, _ = workspace_env
    ref = upload(workspace_env)
    rid = submit(workspace_env, ref)
    responses = []

    def model(request):
        body = json.loads(request.content)
        responses.append(body)
        if len(responses) == 1:
            return httpx.Response(
                200,
                json=completion(
                    None,
                    [
                        tool_call(
                            "save",
                            "workspace_save",
                            json.dumps(dict(artifact_id=ref, computer_mode="team")),
                        )
                    ],
                ),
            )
        assert "denied" in body["messages"][-1]["content"]
        return httpx.Response(200, json=completion("未保存到电脑。"))

    worker = Worker(
        repository,
        harness_factory=lambda _: configured_harness(
            model_settings(), transport=httpx.MockTransport(model)
        ),
    )
    assert worker.run_once()
    card = card_for(repository, repository.get(who, rid))
    assert rpc(
        client,
        headers,
        "threads.answer",
        dict(botId=bot["id"], runId=rid, messageId=card["message_id"], answer="deny"),
    ).is_success
    assert worker.run_once()
    assert calls == [] and folders == {}
    assert repository.get(who, rid).status == RunStatus.SUCCEEDED


def test_full_tool_budget_preserves_existing_agent(workspace_env):
    from agentforge_connectors.workspace import prepare_workspace
    from agentforge_core.execution import ExecutionSnapshot

    who = workspace_env[3]
    config = ExecutionSnapshot(
        model=model_settings().snapshot(),
        tools=tuple(f"tool_{i}@1" for i in range(14)),
        python_image=IMAGE,
        python_continuity=True,
    )
    updated = prepare_workspace(who, config)
    assert not updated.workspace_enabled and updated.tool_references == config.tool_references
    assert updated.approval_tools == ()


def test_repeated_stop_signal_does_not_interrupt_workspace_cleanup(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    state = tmp_path / "slot"
    state.mkdir(mode=0o700)
    script = r"""
import os, time
from pathlib import Path
from agentforge_supervisor import desktop_pool as module
root = Path(os.environ['STOP_TEST_DIR'])
os.environ['AGENTFORGE_DESKTOP_POOL'] = 'pool.sock'
os.environ['AGENTFORGE_WORKSPACE_CHECKPOINTS'] = str(root / 'checkpoint')
os.environ['AGENTFORGE_COMPUTER_IMAGE'] = 'sha256:' + '1' * 64
class Writer:
    def shutdown(self, **kwargs):
        (root / 'cleaning').write_text('started')
        time.sleep(.5)
class Pool:
    def __init__(self, *args):
        self.writer = Writer()
        self.calls = 0
    def recover(self):
        self.calls += 1
        if self.calls == 2:
            (root / 'saved').write_text('cleanup completed')
    def reap(self): pass
    def periodic(self): pass
module.DesktopPool = Pool
module.main()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script], cwd=state, env={**os.environ, "STOP_TEST_DIR": str(state)}
    )
    try:
        for _ in range(100):
            if (state / "pool.sock").exists():
                break
            time.sleep(0.02)
        assert (state / "pool.sock").exists()
        proc.send_signal(signal.SIGTERM)
        for _ in range(100):
            if (state / "cleaning").exists():
                break
            time.sleep(0.01)
        assert (state / "cleaning").exists()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
        assert (state / "saved").read_text() == "cleanup completed"
        assert not (state / "pool.sock").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
