import base64
import hashlib
import json
import struct
import zlib

import httpx
import pytest
import sqlalchemy as sa
from agentforge_core.computer import ActComputer, ScreenImage
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import RuntimeFailure
from agentforge_storage.approvals import ApprovalRepository
from agentforge_storage.schema import runs, tool_calls
from agentforge_supervisor import computer_control
from agentforge_worker.worker import Worker

from tests.model_support import completion, model_settings, tool_call
from tests.test_approval_resume import card_for
from tests.test_workspace_bridge import browser_env, people, submit, workspace_env  # noqa: F401


def screen(value=0):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\0" + bytes([value]) * 48) * 16)) + chunk(b"IEND", b"")
    return ScreenImage(
        width=16,
        height=16,
        sha256=hashlib.sha256(raw).hexdigest(),
        data=base64.b64encode(raw).decode(),
    )


@pytest.fixture
def computer_env(monkeypatch, request):
    monkeypatch.setattr(
        "tests.test_visible_browser.model_settings",
        lambda: model_settings(supports_images=True, image_models=("test-model",)),
    )
    monkeypatch.setenv("AGENTFORGE_COMPUTER_TOOLS_ENABLED", "true")
    env = request.getfixturevalue("workspace_env")
    monkeypatch.delenv("AGENTFORGE_BROWSER_POLICY_FILE")
    original = __import__("tests.test_workspace_bridge", fromlist=["operate"]).operate
    image = screen()
    invoked = []

    def operate(action, arguments, root):
        if action in {"observeComputer", "actComputer", "launchApp"}:
            invoked.append((action, arguments))
            return dict(completed=0 if action == "observeComputer" else 1, image=image.model_dump())
        return original(action, arguments, root)

    monkeypatch.setattr("tests.test_workspace_bridge.operate", operate)
    return env, image, invoked


def vision_worker(repository, respond):
    return Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            model_settings().for_snapshot(snapshot.model), transport=httpx.MockTransport(respond)
        ),
    )


def test_observe_image_then_exact_approval_resume(computer_env, repository):
    env, image, invoked = computer_env
    rid = submit(env)
    config = repository.get(env[3], rid).execution_config
    assert config.computer_enabled
    payloads = []
    args = dict(
        computer_mode="team", screen_sha256=image.sha256, actions=[dict(kind="click", x=2, y=3)]
    )

    def respond(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(
                200, json=completion(None, [tool_call("observe", "computer_observe", "{}")])
            )
        assert (
            body["messages"][-1]["content"][1]["image_url"]["url"]
            == "data:image/png;base64," + image.data
        )
        if len(payloads) == 2:
            return httpx.Response(
                200, json=completion(None, [tool_call("act", "computer_act", json.dumps(args))])
            )
        return httpx.Response(200, json=completion("Observed the desktop"))

    worker = vision_worker(repository, respond)
    worker.run_once()
    run = repository.get(env[3], rid)
    assert run.status == "waiting_input"
    assert [a for a, _ in invoked] == ["observeComputer"]
    with repository.engine.connect() as conn:
        state = conn.execute(
            sa.select(runs.c.execution_checkpoint).where(runs.c.run_id == rid)
        ).scalar_one()
        assert image.data not in json.dumps(state)
        output = conn.execute(
            sa.select(tool_calls.c.output).where(tool_calls.c.run_id == rid)
        ).scalar_one()
        assert output["desktop_image"]["sha256"] == image.sha256
    card = card_for(repository, run)
    ApprovalRepository(repository).answer(env[3], rid, card["message_id"], "allow")
    worker.run_once()
    assert repository.get(env[3], rid).status == "succeeded"
    assert [a for a, _ in invoked] == ["observeComputer", "actComputer"]
    assert invoked[-1][1]["screen_sha256"] == image.sha256
    assert len(payloads) == 3


@pytest.mark.parametrize("reason", ["deny", "cancel", "disabled", "takeover"])
def test_no_action_after_denial_cancellation_revocation_or_takeover(
    computer_env, repository, monkeypatch, reason
):
    env, image, invoked = computer_env
    rid = submit(env)
    requests = []

    def respond(request):
        requests.append(1)
        return httpx.Response(
            200,
            json=completion(
                None,
                [
                    tool_call(
                        "launch",
                        "launch_app",
                        json.dumps(dict(application="browser", computer_mode="team")),
                    )
                ],
            ),
        )

    worker = vision_worker(repository, respond)
    worker.run_once()
    run = repository.get(env[3], rid)
    assert run.status == "waiting_input"
    if reason == "cancel":
        repository.cancel(env[3], rid)
    else:
        card = card_for(repository, run)
        ApprovalRepository(repository).answer(
            env[3], rid, card["message_id"], "deny" if reason == "deny" else "allow"
        )
        if reason == "disabled":
            monkeypatch.setenv("AGENTFORGE_COMPUTER_TOOLS_ENABLED", "false")
        elif reason == "takeover":
            # The reserved desktop rejects a takeover before reaching the fixed operation.
            env[7].append(lambda slot, body: setattr(slot, "controller", "manual-user"))
    worker.run_once()
    assert invoked == []
    assert repository.get(env[3], rid).status in {"failed", "cancelled"}


def test_text_only_or_mixed_backup_disables_new_computer_tools(computer_env):
    from agentforge_connectors.workspace import prepare_workspace

    from tests.test_python_execution import IMAGE

    env, _, _ = computer_env
    base = ExecutionSnapshot(
        model=model_settings().snapshot(), python_image=IMAGE, python_continuity=True
    )
    assert not prepare_workspace(env[3], base).computer_enabled
    vision = base.model_copy(
        update={
            "model": model_settings(supports_images=True).snapshot(),
            "model_backups": (base.model,),
        }
    )
    assert not prepare_workspace(env[3], vision).computer_enabled
    assert not ExecutionSnapshot().computer_enabled


def test_invalid_or_stale_batch_never_executes_prefix(monkeypatch):
    image = screen()
    calls = []
    monkeypatch.setattr(computer_control, "capture", lambda: image.model_dump())
    monkeypatch.setattr(computer_control.subprocess, "run", lambda *a, **k: calls.append(a))
    valid = dict(kind="click", x=2, y=2)
    reply = computer_control.operate("actComputer", dict(screen_sha256="a" * 64, actions=[valid]))
    assert reply == {"error": "computer_screen_changed"}
    with pytest.raises(ValueError):
        computer_control.operate(
            "actComputer",
            dict(screen_sha256=image.sha256, actions=[valid, dict(kind="click", x=999, y=999)]),
        )
    assert calls == []


def test_unknown_post_action_screenshot_failure_is_not_safe_retry(monkeypatch):
    image = screen()
    captures = iter([image.model_dump(), None])

    def capture():
        result = next(captures)
        if result is None:
            raise TimeoutError()
        return result

    calls = []
    monkeypatch.setattr(computer_control, "capture", capture)
    monkeypatch.setattr(computer_control.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(TimeoutError):
        computer_control.operate(
            "actComputer", dict(screen_sha256=image.sha256, actions=[dict(kind="key", key="Tab")])
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "actions",
    [
        [dict(kind="shell", command="id")],
        [dict(kind="key", key="ctrl+alt+t")],
        [dict(kind="type", text="x\n")],
    ],
)
def test_protocol_rejects_unbounded_desktop_commands(actions):
    with pytest.raises(ValueError):
        ActComputer(computer_mode="team", screen_sha256="a" * 64, actions=actions)


def test_image_integrity_and_model_capability_checked_before_http():
    from agentforge_runtime.chat_model import ChatCompletionsModel

    image = screen()
    with pytest.raises(ValueError):
        ScreenImage.model_validate({**image.model_dump(), "sha256": "a" * 64})
    model = ChatCompletionsModel(
        model_settings(),
        transport=httpx.MockTransport(lambda _: pytest.fail("Text-only route was called")),
    )
    try:
        with pytest.raises(RuntimeFailure) as failure:
            model.complete(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + image.data},
                            }
                        ],
                    }
                ],
                [],
            )
        assert failure.value.code == RunErrorCode.MODEL_CONFIGURATION_MISMATCH
    finally:
        model.close()


@pytest.mark.parametrize("error_code", ["computer_screen_changed", "computer_application_missing"])
def test_known_pre_action_failure_is_audited_without_replay(
    computer_env, repository, monkeypatch, error_code
):
    from agentforge_storage.schema import external_effects
    from agentforge_supervisor.workspace_files import WorkspaceFileError

    env, image, invoked = computer_env
    dispatched = []

    def operate(*args):
        dispatched.append(1)
        raise WorkspaceFileError(error_code)

    monkeypatch.setattr("tests.test_workspace_bridge.operate", operate)
    rid = submit(env)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if error_code == "computer_screen_changed":
                name, args = (
                    "computer_act",
                    dict(
                        computer_mode="team",
                        screen_sha256=image.sha256,
                        actions=[dict(kind="key", key="Tab")],
                    ),
                )
            else:
                name, args = "launch_app", dict(computer_mode="team", application="calculator")
            return httpx.Response(
                200, json=completion(None, [tool_call("action", name, json.dumps(args))])
            )
        assert json.loads(body["messages"][-1]["content"])["error"] == error_code
        assert "No desktop action" in body["messages"][-1]["content"]
        return httpx.Response(200, json=completion("The operation did not execute."))

    worker = vision_worker(repository, respond)
    worker.run_once()
    run = repository.get(env[3], rid)
    card = card_for(repository, run)
    ApprovalRepository(repository).answer(env[3], rid, card["message_id"], "allow")
    worker.run_once()
    assert repository.get(env[3], rid).status == "succeeded"
    assert dispatched == [1]
    with repository.engine.connect() as conn:
        assert conn.execute(
            sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
        ).scalars().all() == ["failed"]
        assert conn.execute(
            sa.select(tool_calls.c.status).where(tool_calls.c.run_id == rid)
        ).scalars().all() == ["failed"]


def test_cancel_after_action_does_not_publish_screenshot_or_replay(
    computer_env, repository, monkeypatch
):
    from agentforge_storage.schema import external_effects

    env, image, invoked = computer_env
    rid = submit(env)
    dispatched = []

    def operate(*args):
        dispatched.append(1)
        repository.cancel(env[3], rid)
        return dict(completed=1, image=image.model_dump())

    monkeypatch.setattr("tests.test_workspace_bridge.operate", operate)

    def respond(request):
        return httpx.Response(
            200,
            json=completion(
                None,
                [
                    tool_call(
                        "launch",
                        "launch_app",
                        json.dumps(dict(application="browser", computer_mode="team")),
                    )
                ],
            ),
        )

    worker = vision_worker(repository, respond)
    worker.run_once()
    run = repository.get(env[3], rid)
    card = card_for(repository, run)
    ApprovalRepository(repository).answer(env[3], rid, card["message_id"], "allow")
    worker.run_once()
    assert repository.get(env[3], rid).status == "cancelled"
    assert dispatched == [1]
    assert not worker.run_once()
    with repository.engine.connect() as conn:
        assert conn.execute(
            sa.select(tool_calls.c.output).where(tool_calls.c.run_id == rid)
        ).scalars().all() == [None]
        assert conn.execute(
            sa.select(external_effects.c.status).where(external_effects.c.run_id == rid)
        ).scalars().all() == ["dispatched"]


def test_image_loader_rejects_other_runs_and_lost_claim(repository, scope):
    from agentforge_core.models import RunConflict
    from agentforge_core.tools import ToolInvocation

    run = repository.create(scope, "image")
    # Populate via the same journal transaction, with active run ownership.
    claim = repository.claim_next(lease_seconds=30)
    assert claim is not None
    ticket = repository.start_tool(claim, ToolInvocation("observe", "computer_observe", 1, "{}"))
    repository.complete_tool(
        claim, ticket, output={"desktop_image": screen().model_dump()}, content="{}"
    )
    repository.create(scope, "other")
    second = repository.claim_next(lease_seconds=30)
    assert second is not None
    with pytest.raises(RunConflict):
        repository.desktop_image(second, "observe")
    assert repository.desktop_image(claim, "observe").sha256 == screen().sha256
    repository.cancel(scope, run.run_id)
    with pytest.raises(RunConflict):
        repository.desktop_image(claim, "observe")


def test_context_keeps_two_distinct_screens_without_changing_checkpoint():
    from types import SimpleNamespace

    from agentforge_runtime.computer_tools import with_latest_screen

    images = {"a": screen(10), "b": screen(20), "c": screen(30), "d": screen(30)}
    messages = []
    for cid, image in images.items():
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [tool_call(cid, "computer_observe", "{}")]},
                {
                    "role": "tool",
                    "tool_call_id": cid,
                    "content": json.dumps({"screen": image.metadata()}),
                },
            ]
        )
    saved = json.dumps(messages)
    loaded = []

    def load(cid):
        loaded.append(cid)
        return images[cid]

    executor = SimpleNamespace(
        checkpoint=lambda: True, schemas=lambda: [], journal=SimpleNamespace(desktop_image=load)
    )
    selected = with_latest_screen(
        messages, executor, model_settings(supports_images=True).snapshot()
    )
    assert loaded == ["b", "d"]
    assert len(selected) == len(messages) + 2
    assert json.dumps(messages) == saved
