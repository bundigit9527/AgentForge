"""Python generation uses real run/lease/attachment transactions, never host eval."""

import base64
import io
import json
import threading
import zipfile
from dataclasses import asdict, replace
from unittest.mock import patch

import httpx
import pytest
from agentforge_api.app import create_app
from agentforge_connectors.python_execution import PythonPolicy, PythonService
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode, RunNotFound
from agentforge_core.python_execution import (
    PPTX_MIME,
    EncodedFile,
    GeneratedFile,
    PythonArguments,
    PythonResponse,
)
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_runtime.python_tools import with_python_tool
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_storage.artifacts import ArtifactRepository
from agentforge_supervisor.app import create_app as supervisor_app
from agentforge_supervisor.python_runner import PythonComputer
from agentforge_worker.tools import RunToolJournal
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_orpc import create_bot, rpc

IMAGE = "sha256:" + "1" * 64


@pytest.fixture
def python_grant(monkeypatch, tmp_path, scope):
    path = tmp_path / "python.json"
    grant = asdict(scope)
    grant["users"] = [grant.pop("user_id")]
    path.write_text(json.dumps({"grants": [grant]}))
    monkeypatch.setenv("AGENTFORGE_PYTHON_POLICY_FILE", str(path))
    monkeypatch.setenv("AGENTFORGE_PYTHON_IMAGE", IMAGE)
    monkeypatch.setenv("AGENTFORGE_MCP_COMPUTER_TOKEN", "x" * 32)
    monkeypatch.setenv("AGENTFORGE_PYTHON_ENDPOINT", "http://127.0.0.1:8766")
    return path


def ppt_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return output.getvalue()


def result(name="方案.pptx", data=None):
    return PythonResponse(
        exit_code=0,
        stdout="checked",
        stderr="",
        files=[EncodedFile(name=name, data=base64.b64encode(data or ppt_bytes()).decode())],
    )


def test_chat_auto_skill_script_repair_and_file_download(
    settings,
    repository,
    headers,
    scope,
    python_grant,
):
    gateway = model_settings()
    calls = []

    def model(request):
        body = json.loads(request.content)
        step = len(calls)
        calls.append(body)
        if step == 0:
            assert "python_execute" in str(body["tools"])
            assert "builtin:presentation@2" in str(body["messages"])
            tool = tool_call("recipe", "skill_read", '{"reference":"builtin:presentation@2"}')
        elif step in (1, 2):
            if step == 2:
                assert "NameError" in body["messages"][-1]["content"]
            tool = tool_call(
                "code" + str(step),
                "python_execute",
                json.dumps({"code": "broken" if step == 1 else "fixed", "exports": ["方案.pptx"]}),
            )
        else:
            receipt = json.loads(body["messages"][-1]["content"])
            assert len(receipt["files"]) == 1
            assert receipt["files"][0]["name"] == "方案.pptx"
            assert receipt["files"][0]["mime_type"] == PPTX_MIME
            assert receipt["files"][0]["size"] == len(ppt_bytes())
            assert receipt["files"][0]["artifact_id"]
            assert "base64" not in str(body["messages"][-1])
            return httpx.Response(200, json=completion("已生成可编辑 PPT，见附件。"))
        return httpx.Response(200, json=completion(None, [tool]))

    def compute(self, arguments):
        self.authorized()
        if arguments["code"] == "broken":
            return PythonResponse(exit_code=1, stdout="", stderr="NameError: correct code")
        return result()

    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)["id"]
        data = dict(botId=bot, text="把之前的公司AI架构整理成PPT", clientNonce="ppt")
        sent = rpc(client, headers, "threads.send", data)
        assert sent.is_success, sent.text
        rid = sent.json()["json"]["runId"]
        worker = Worker(
            repository,
            harness_factory=lambda _: configured_harness(
                gateway, transport=httpx.MockTransport(model)
            ),
        )
        with patch.object(PythonService, "execute", compute):
            assert worker.run_once()
        assert repository.get(scope, rid).status.value == "succeeded"
        assert len(calls) == 4
        files = rpc(client, headers, "artifacts.list", {"botId": bot}).json()["json"]
        assert len(files) == 1 and files[0]["mimeType"] == PPTX_MIME
        download = rpc(
            client, headers, "artifacts.get", {"botId": bot, "artifactId": files[0]["id"]}
        ).json()["json"]
        assert base64.b64decode(download["contentBase64"]) == ppt_bytes()
        state = rpc(client, headers, "threads.get", {"botId": bot}).json()["json"]
        assert any(
            b.get("artifactId") == files[0]["id"] for m in state["messages"] for b in m["blocks"]
        )
        assert rpc(client, headers, "threads.send", data).json() == sent.json()
        assert not worker.run_once()
        with pytest.raises(RunNotFound):
            ArtifactRepository(repository).get(replace(scope, user_id="other"), files[0]["id"])


def test_disabled_and_old_runs_never_gain_python(settings, repository, headers, scope, monkeypatch):
    monkeypatch.delenv("AGENTFORGE_PYTHON_POLICY_FILE", raising=False)
    gateway = model_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)["id"]
        sent = rpc(client, headers, "threads.send", {"botId": bot, "text": "生成PPT"})
        config = repository.get(scope, sent.json()["json"]["runId"]).execution_config
        assert config.python_image is None
        assert not any(s.manifest.skill_id == "presentation" for s in config.available_skills)
        assert (
            ExecutionSnapshot.model_validate(
                {"model": gateway.snapshot().model_dump()}
            ).python_image
            is None
        )
        with pytest.raises(RuntimeFailure):
            ToolExecutor(("python_execute@1",), allowed=("python_execute@1",))


def submitted(settings, repository, headers, python_grant):
    gateway = model_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        bot = create_bot(client, headers)["id"]
        sent = rpc(client, headers, "threads.send", {"botId": bot, "text": "生成PPT"})
        assert sent.is_success, sent.text
        return sent.json()["json"]["runId"]


@pytest.mark.parametrize("stop", ["cancel", "lease", "revoke"])
def test_cancel_lease_loss_or_revoke_discards_file(
    settings,
    repository,
    headers,
    scope,
    python_grant,
    clock,
    stop,
):
    rid = submitted(settings, repository, headers, python_grant)

    def compute(self, args):
        if stop == "cancel":
            repository.cancel(scope, rid)
        elif stop == "lease":
            clock.advance(100)
        else:
            python_grant.write_text('{"grants":[]}')
        return result()

    def model(request):
        return httpx.Response(
            200,
            json=completion(
                None,
                [
                    tool_call(
                        "code", "python_execute", '{"code":"print(1)","exports":["方案.pptx"]}'
                    )
                ],
            ),
        )

    gateway = model_settings()
    worker = Worker(
        repository,
        harness_factory=lambda _: configured_harness(gateway, transport=httpx.MockTransport(model)),
    )
    with patch.object(PythonService, "execute", compute):
        worker.run_once()
    assert ArtifactRepository(repository).listing(scope) == []
    assert repository.get(scope, rid).status.value != "succeeded"


def test_tool_replay_does_not_regenerate_or_duplicate_file(
    settings,
    repository,
    headers,
    scope,
    python_grant,
):
    submitted(settings, repository, headers, python_grant)
    claim = repository.claim_next()
    service = PythonService(scope, IMAGE, lambda: repository.owns_claim(claim))
    registry = with_python_tool(BUILTIN_TOOLS, service)
    executor = ToolExecutor(
        ("python_execute@1",),
        registry=registry,
        allowed=("python_execute@1",),
        journal=RunToolJournal(repository, claim),
        checkpoint=lambda: repository.owns_claim(claim),
    )
    prepared = executor.prepare("same", "python_execute", '{"code":"x","exports":["方案.pptx"]}')
    with patch.object(service, "execute", return_value=result()) as execute:
        a = executor.execute(prepared)
        assert executor.execute(prepared) == a
        assert execute.call_count == 1
    assert len(ArtifactRepository(repository).listing(scope)) == 1


@pytest.mark.parametrize(
    "exports", [["../escape.pptx"], ["/secret.pptx"], ["x.exe"], ["x.pptx"] * 2]
)
def test_invalid_paths_never_execute(exports):
    with pytest.raises(ValueError):
        PythonArguments(code="x", exports=exports)


def test_false_office_output_rejected():
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        GeneratedFile.decode(EncodedFile(name="fake.pptx", data="aGVsbG8="))


def test_endpoint_authorization_image_and_input_bounds(monkeypatch, python_grant):
    monkeypatch.setenv("AGENTFORGE_COMPUTER_IMAGE", IMAGE)
    payload = {"image": IMAGE, "code": "print(1)", "exports": []}
    with (
        TestClient(supervisor_app()) as client,
        patch.object(
            PythonComputer,
            "execute",
            return_value=PythonResponse(exit_code=0, stdout="1", stderr=""),
        ) as execute,
    ):
        assert client.post("/python/execute", json=payload).status_code == 401
        auth = {"Authorization": "Bearer " + "x" * 32}
        assert (
            client.post(
                "/python/execute", headers=auth, json={**payload, "image": "sha256:" + "2" * 64}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/python/execute", headers=auth, json={**payload, "exports": ["../x.pptx"]}
            ).status_code
            == 422
        )
        assert not execute.called
        assert client.post("/python/execute", headers=auth, json=payload).status_code == 200
        assert (
            client.post("/python/execute", headers=auth, content=b"x" * 14_100_001).status_code
            == 413
        )


def test_grant_transport_rechecks_current_permission(scope, python_grant):
    def reply(request):
        assert request.url.path == "/python/execute"
        assert json.loads(request.content)["image"] == IMAGE
        python_grant.write_text('{"grants":[]}')
        return httpx.Response(200, json=result().model_dump())

    service = PythonService(scope, IMAGE, lambda: True, transport=httpx.MockTransport(reply))
    with pytest.raises(RuntimeFailure) as exc:
        service.execute({"code": "x", "exports": ["方案.pptx"]})
    assert exc.value.code == RunErrorCode.TOOL_NOT_ALLOWED
    assert not PythonPolicy.from_env().permits(scope)


def test_no_long_running_request_after_cancel(scope, python_grant):
    checks = [True]
    service = PythonService(scope, IMAGE, lambda: checks[0])
    checks[0] = False
    with pytest.raises(ExecutionStopped):
        service.execute({"code": "x", "exports": []})


def test_pinned_image_validated_before_docker():
    computer = PythonComputer(IMAGE)
    with patch("subprocess.Popen") as run, pytest.raises(ValueError):
        computer.execute(
            {"image": "sha256:" + "2" * 64, "code": "x", "exports": []}, threading.Event()
        )
    assert not run.called
