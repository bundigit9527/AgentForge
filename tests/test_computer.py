import json
import os
import subprocess
from unittest.mock import patch

import httpx
import httpx2
import pytest
from agentforge_api.app import create_app as create_api
from agentforge_connectors.mcp import McpService
from agentforge_connectors.mcp_config import McpServer, McpSettings
from agentforge_runtime.chat_harness import configured_harness
from agentforge_supervisor.app import create_app
from agentforge_supervisor.computer import DockerComputer
from agentforge_worker.worker import Worker
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_orpc import rpc

IMAGE = "sha256:" + "1" * 64
TOKEN_ENV = "AGENTFORGE_MCP_COMPUTER_TOKEN"
SUMMARY = {"count": 3, "minimum": -3, "maximum": 15, "total": 18, "mean": 6.0}


def test_supervisor_authentication(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match="TOKEN"):
        create_app()
    monkeypatch.setenv(TOKEN_ENV, "x" * 32)
    monkeypatch.setenv("AGENTFORGE_COMPUTER_IMAGE", IMAGE)
    with TestClient(create_app()) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert (
            client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code
            == 401
        )


@pytest.mark.parametrize("values", [[], [True], ["1"], [10**12 + 1], [1] * 513])
def test_invalid_input_never_starts_docker(values):
    with patch("subprocess.run") as runner, pytest.raises(ValueError):
        DockerComputer(IMAGE).summarize(values)
    runner.assert_not_called()


def test_timeout_cleans_only_this_container_and_releases_slot():
    computer = DockerComputer(IMAGE)
    with patch(
        "subprocess.run", side_effect=[subprocess.TimeoutExpired("docker", 12), None]
    ) as runner:
        with pytest.raises(RuntimeError, match="execution failed"):
            computer.summarize([1])
    command = runner.call_args_list[0].args[0]
    name = command[command.index("--name") + 1]
    assert runner.call_args_list[1].args[0] == ["docker", "rm", "--force", name]
    assert computer.slots.acquire(blocking=False)
    assert computer.slots.acquire(blocking=False)
    with patch("subprocess.run") as runner, pytest.raises(RuntimeError, match="busy"):
        computer.summarize([1])
    runner.assert_not_called()


def test_operator_must_pin_image():
    with pytest.raises(ValueError, match="image ID"):
        DockerComputer("python:latest")


def test_chat_approval_to_computer_and_back(monkeypatch, scope, settings, repository, headers):
    # Set this explicit test variable only for a local Docker integration run.
    live_image = os.environ.get("AGENTFORGE_TEST_COMPUTER_IMAGE")
    monkeypatch.setenv("AGENTFORGE_COMPUTER_IMAGE", live_image or IMAGE)
    monkeypatch.setenv(TOKEN_ENV, "offline-auth-test-" * 2)
    runs = []

    def fake_docker(command, **kwargs):
        runs.append(command)
        assert json.loads(kwargs["input"]) == [-3, 6, 15]
        for flag in (
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--user=65534:65534",
            "--security-opt=no-new-privileges",
            "--pull=never",
            "--rm",
        ):
            assert flag in command
        assert not {"--privileged", "-v", "--mount", "--env-file", "--env"} & set(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(SUMMARY).encode())

    if not live_image:
        monkeypatch.setattr(subprocess, "run", fake_docker)
    supervisor = create_app()
    config = McpServer(
        server_id="computer",
        tenant_id=scope.tenant_id,
        space_id=scope.space_id,
        users=(scope.user_id,),
        url="http://127.0.0.1:8766/mcp",
        allow_insecure_http=True,
        token_env=TOKEN_ENV,
        read_tools={"summarize_numbers": 1},
        timeout_seconds=20,
    )
    service = McpService(
        McpSettings(servers=(config,)),
        transport_factory=lambda: httpx2.ASGITransport(app=supervisor),
    )
    gateway = model_settings()
    model_calls = []

    def model(request):
        body = json.loads(request.content)
        model_calls.append(body)
        if len(model_calls) == 1:
            name = body["tools"][0]["function"]["name"]
            return httpx.Response(
                200, json=completion(None, [tool_call(name=name, arguments='{"values":[-3,6,15]}')])
            )
        assert json.loads(body["messages"][-1]["content"])["structured"] == SUMMARY
        return httpx.Response(200, json=completion("平均值为 6。"))

    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(supervisor.router.lifespan_context(supervisor)),
        TestClient(
            create_api(settings, repository, model_settings=gateway, mcp_service=service)
        ) as client,
    ):
        # Keep the in-process ASGI transport on the server's lifespan loop.
        monkeypatch.setattr(service, "_run", lambda *args: portal.call(service.exchange, *args))
        catalog = client.get("/api/v1/mcp/servers/computer/tools", headers=headers)
        assert catalog.status_code == 200, catalog.text
        tool = catalog.json()["items"][0]
        created = client.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "name": "统计助手",
                "tools": [],
                "approval_tools": [tool["reference"]],
                "mcp_tools": [{k: tool[k] for k in ("server_id", "tool_name", "sha256")}],
            },
        )
        assert created.status_code == 201, created.text
        bot = created.json()["agent_id"]
        sent = rpc(client, headers, "threads.send", {"botId": bot, "text": "统计 -3、6、15"})
        run_id = sent.json()["json"]["runId"]
        worker = Worker(
            repository,
            harness_factory=lambda _: configured_harness(
                gateway, transport=httpx.MockTransport(model)
            ),
            mcp_service=service,
        )
        assert worker.run_once()
        snapshot = rpc(client, headers, "threads.get", {"botId": bot}).json()["json"]
        assert snapshot["run"]["status"] == "waiting_input"
        assert not runs
        card = snapshot["messages"][-1]
        response = rpc(
            client,
            headers,
            "threads.answer",
            {"botId": bot, "runId": run_id, "messageId": card["id"], "answer": "allow"},
        )
        assert response.status_code == 200
        assert worker.run_once()
        assert repository.get(scope, run_id).result == "平均值为 6。"
        audit = repository.list_tool_calls(scope, run_id)
        assert len(audit) == 1 and audit[0]["output"]["structured"] == SUMMARY
        if not live_image:
            assert len(runs) == 1
