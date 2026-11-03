"""Real SDK + HTTP wire contracts; no sockets, credentials or live external services."""

import json
import logging
from copy import deepcopy
from dataclasses import replace

import anyio
import httpx
import httpx2
import pytest
from agentforge_api.app import create_app
from agentforge_connectors.mcp import McpService
from agentforge_connectors.mcp_config import McpServer, McpSettings
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.mcp import McpCancelled, McpFailure, McpToolSnapshot, validate_schema
from agentforge_core.models import RunStatus
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.errors import RuntimeFailure
from agentforge_runtime.mcp_tools import with_mcp_tools
from agentforge_runtime.tools import BUILTIN_TOOLS, ToolExecutor
from agentforge_storage.repository import RunRepository
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import people as _people

people = _people
SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 200}},
    "required": ["query"],
    "additionalProperties": False,
}


class Remote:
    def __init__(self):
        self.requests = []
        self.definitions = [
            {
                "name": "search",
                "description": "Search product information",
                "inputSchema": deepcopy(SCHEMA),
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            }
        ]
        self.result = {"content": [{"type": "text", "text": "Product A: 42"}], "isError": False}
        self.next_cursor = None
        self.session = 0
        self.call_hook = None

    async def handle(self, request):
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            return httpx2.Response(204)
        data = json.loads(request.content)
        self.requests.append((data, dict(request.headers)))
        method = data["method"]
        if "id" not in data:
            return httpx2.Response(202)
        headers = {}
        if method == "server/discover":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "error": {"code": -32601, "message": "unsupported"},
                },
            )
        if method == "initialize":
            self.session += 1
            headers["Mcp-Session-Id"] = "session-" + str(self.session)
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline-mcp", "version": "1"},
                "instructions": "private-server-instructions-do-not-promote",
            }
        elif method == "tools/list":
            result = {"tools": self.definitions}
            if self.next_cursor is not None:
                result["nextCursor"] = self.next_cursor
        elif method == "tools/call":
            if self.call_hook:
                await self.call_hook()
            result = self.result
        else:
            raise AssertionError(method)
        return httpx2.Response(
            200, json={"jsonrpc": "2.0", "id": data["id"], "result": result}, headers=headers
        )

    def calls(self):
        return [data for data, _ in self.requests if data["method"] == "tools/call"]


def make_service(scope, remote, **changes):
    server = McpServer(
        **dict(
            server_id="products",
            tenant_id=scope.tenant_id,
            space_id=scope.space_id,
            users=(scope.user_id,),
            url="https://mcp.example/mcp",
            read_tools={"search": 1},
            **changes,
        )
    )
    return McpService(
        McpSettings(servers=(server,)),
        transport_factory=lambda: httpx2.MockTransport(remote.handle),
    )


def selection(snapshot):
    return {
        "server_id": snapshot.server_id,
        "tool_name": snapshot.tool_name,
        "sha256": snapshot.sha256,
    }


def test_sdk_legacy_handshake_catalog_and_result(scope, monkeypatch):
    remote = Remote()
    service = make_service(scope, remote, token_env="AGENTFORGE_MCP_PRODUCTS_TOKEN")
    monkeypatch.setenv("AGENTFORGE_MCP_PRODUCTS_TOKEN", "fixture-token-only")
    tool = service.catalog(scope, "products")[0]
    assert tool.reference.startswith("mcp_products_") and tool.version == 1
    result = service.call(scope, tool, {"query": "A"}, lambda: True)
    assert result == {"text": "Product A: 42", "structured": None}
    assert len(remote.calls()) == 1
    assert remote.session == 2  # Independent connections for discovery / execution.
    assert all(h["authorization"] == "Bearer fixture-token-only" for _, h in remote.requests)
    assert "fixture-token-only" not in tool.model_dump_json()
    assert "https://" not in tool.model_dump_json()
    assert "private-server-instructions" not in tool.model_dump_json()
    assert any(h.get("mcp-protocol-version") == "2025-11-25" for _, h in remote.requests)


@pytest.mark.parametrize("json_response", [True, False])
def test_current_official_sdk_server_over_streamable_http(scope, json_response):
    server = MCPServer("Product catalog")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    def search(query: str) -> str:
        return "Product " + query

    app = server.streamable_http_app(json_response=json_response, stateless_http=True)
    config = McpServer(
        server_id="products",
        tenant_id=scope.tenant_id,
        space_id=scope.space_id,
        users=(scope.user_id,),
        url="http://127.0.0.1:8001/mcp",
        allow_insecure_http=True,
        read_tools={"search": 1},
    )
    service = McpService(
        McpSettings(servers=(config,)), transport_factory=lambda: httpx2.ASGITransport(app=app)
    )

    async def exercise():
        async with app.router.lifespan_context(app):
            tools = await service.exchange(config, None, None, lambda: True)
            result = await service.exchange(config, tools[0], {"query": "A"}, lambda: True)
            assert result["text"] == "Product A"
            assert result["structured"] == {"result": "Product A"}

    anyio.run(exercise)


def test_api_binding_worker_model_result_and_audit(settings, repository, scope, headers):
    remote = Remote()
    service = make_service(scope, remote)
    gateway = model_settings()
    with TestClient(
        create_app(settings, repository, model_settings=gateway, mcp_service=service)
    ) as client:
        assert client.get("/api/v1/mcp/servers").status_code == 401
        assert client.get("/api/v1/mcp/servers", headers=headers).json() == {
            "items": [{"server_id": "products", "tools": ["search"]}]
        }
        item = client.get("/api/v1/mcp/servers/products/tools", headers=headers).json()["items"][0]
        body = {
            "name": "MCP reader",
            "tools": [],
            "mcp_tools": [{k: item[k] for k in ("server_id", "tool_name", "sha256")}],
        }
        created = client.post("/api/v1/agents", headers=headers, json=body)
        assert created.status_code == 201, created.text
        agent = created.json()
        snapshot = McpToolSnapshot.model_validate(agent["execution_config"]["mcp_tools"][0])
        thread = client.post(
            "/api/v1/threads", headers=headers, json={"agent_id": agent["agent_id"]}
        ).json()
        run = client.post(
            "/api/v1/runs",
            headers=headers,
            json={"thread_id": thread["thread_id"], "prompt": "Find A"},
        ).json()
        requests = []

        def model(request):
            data = json.loads(request.content)
            requests.append(data)
            if len(requests) == 1:
                assert data["tools"][0]["function"]["name"] == snapshot.name
                assert data["tools"][0]["function"]["parameters"] == SCHEMA
                assert "private-server-instructions" not in json.dumps(data)
                return httpx.Response(
                    200,
                    json=completion(
                        None, [tool_call(name=snapshot.name, arguments=json.dumps({"query": "A"}))]
                    ),
                )
            output = json.loads(data["messages"][-1]["content"])
            assert output["text"] == "Product A: 42"
            return httpx.Response(200, json=completion("A is 42"))

        with configured_harness(gateway, transport=httpx.MockTransport(model)) as harness:
            assert Worker(repository, harness, mcp_service=service).run_once()
        assert repository.get(scope, run["run_id"]).result == "A is 42"
        calls = client.get(f"/api/v1/runs/{run['run_id']}/tool-calls", headers=headers).json()[
            "items"
        ]
        assert len(calls) == 1 and calls[0]["status"] == "succeeded"
        assert calls[0]["name"] == snapshot.name and calls[0]["arguments"] == {"query": "A"}
        assert calls[0]["output"]["text"] == "Product A: 42"
        events = repository.list_events(scope, run["run_id"])
        assert [e.event_type for e in events] == [
            "queued",
            "running",
            "model_completed",
            "tool_started",
            "tool_completed",
            "model_completed",
            "succeeded",
        ]
        assert repository.get(scope, run["run_id"]).execution_config.mcp_tools == (snapshot,)
        remote.definitions[0]["description"] = "Changed after binding"
        stale = client.post("/api/v1/agents", headers=headers, json=body)
        assert stale.status_code == 409
        assert client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers).json() == agent


@pytest.mark.parametrize("change", ["tenant", "space", "user"])
def test_scope_cannot_discover_or_call_other_mcp_service(scope, change):
    remote = Remote()
    service = make_service(scope, remote)
    other = replace(scope, **{change + "_id": "other"})
    assert service.servers(other) == []
    with pytest.raises(McpFailure, match="mcp_not_allowed"):
        service.catalog(other, "products")
    assert remote.requests == []


@pytest.mark.parametrize("change", ["schema", "description", "version", "route", "revoke"])
def test_pinned_definition_and_current_grant_block_changed_tools(scope, change):
    remote = Remote()
    service = make_service(scope, remote)
    snapshot = service.catalog(scope, "products")[0]
    if change == "schema":
        remote.definitions[0]["inputSchema"]["properties"]["query"]["maxLength"] = 100
    elif change == "description":
        remote.definitions[0]["description"] = "changed"
    else:
        server = service.settings.servers[0]
        values = {
            "version": {"read_tools": {"search": 2}},
            "route": {"url": "https://changed.example/mcp"},
            "revoke": {"users": ("somebody-else",)},
        }[change]
        service.settings = McpSettings(servers=(server.model_copy(update=values),))
    with pytest.raises(McpFailure, match="mcp_(definition_changed|not_allowed)"):
        service.call(scope, snapshot, {"query": "A"}, lambda: True)
    assert not remote.calls()


@pytest.mark.parametrize(
    "arguments", [{"query": 1}, {"query": "A", "extra": True}, {}, {"query": "x" * 201}]
)
def test_json_schema_arguments_rejected_before_remote_call(scope, arguments):
    remote = Remote()
    service = make_service(scope, remote)
    snapshot = service.catalog(scope, "products")[0]
    registry = with_mcp_tools(BUILTIN_TOOLS, service, scope, (snapshot,), lambda: True)
    executor = ToolExecutor(
        (snapshot.reference,),
        registry=registry,
        allowed=(snapshot.reference,),
        read_allowed=(snapshot.reference,),
    )
    with pytest.raises(RuntimeFailure, match="tool_invalid_arguments"):
        executor.prepare("call-1", snapshot.name, json.dumps(arguments))
    assert not remote.calls()


def test_remote_read_requires_separate_runtime_grant(scope):
    service = make_service(scope, Remote())
    snapshot = service.catalog(scope, "products")[0]
    registry = with_mcp_tools(BUILTIN_TOOLS, service, scope, (snapshot,), lambda: True)
    with pytest.raises(RuntimeFailure, match="tool_not_allowed"):
        ToolExecutor((snapshot.reference,), registry=registry, allowed=(snapshot.reference,))
    with pytest.raises(ValueError):
        ExecutionSnapshot(mcp_tools=(snapshot,))  # Mock mode must remain offline.


@pytest.mark.parametrize(
    "kind", ["missing_hints", "write", "non_idempotent", "schema", "duplicate", "pagination"]
)
def test_bad_catalog_fails_closed(scope, kind):
    remote = Remote()
    if kind == "missing_hints":
        remote.definitions[0].pop("annotations")
    elif kind == "write":
        remote.definitions[0]["annotations"]["readOnlyHint"] = False
    elif kind == "non_idempotent":
        remote.definitions[0]["annotations"]["idempotentHint"] = False
    elif kind == "schema":
        remote.definitions[0]["inputSchema"]["$ref"] = "https://untrusted.example"
    elif kind == "duplicate":
        remote.definitions.append(deepcopy(remote.definitions[0]))
    else:
        remote.next_cursor = "repeat"
    with pytest.raises(McpFailure):
        make_service(scope, remote).catalog(scope, "products")
    assert not remote.calls()


@pytest.mark.parametrize("kind", ["is_error", "image", "resource", "empty", "large"])
def test_unsupported_results_never_become_a_successful_tool(scope, kind):
    remote = Remote()
    service = make_service(scope, remote)
    snapshot = service.catalog(scope, "products")[0]
    if kind == "is_error":
        remote.result["isError"] = True
    elif kind == "image":
        remote.result["content"] = [{"type": "image", "data": "eA==", "mimeType": "image/png"}]
    elif kind == "resource":
        remote.result["content"] = [
            {"type": "resource_link", "uri": "file:///private/data", "name": "secret"}
        ]
    elif kind == "empty":
        remote.result["content"] = []
    else:
        remote.result["content"][0]["text"] = "x" * 21000
    with pytest.raises(McpFailure):
        service.call(scope, snapshot, {"query": "A"}, lambda: True)
    assert len(remote.calls()) == 1


def test_remote_error_is_audited_without_echoing_body(repository, scope, caplog):
    remote = Remote()
    service = make_service(scope, remote)
    tool = service.catalog(scope, "products")[0]
    remote.result = {
        "isError": True,
        "content": [{"type": "text", "text": "private-upstream-message"}],
    }
    gateway = model_settings()
    run = repository.create(
        scope,
        "task",
        execution_config=ExecutionSnapshot(model=gateway.snapshot(), tools=(), mcp_tools=(tool,)),
    )
    with configured_harness(
        gateway,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=completion(None, [tool_call(name=tool.name, arguments='{"query":"A"}')])
            )
        ),
    ) as harness:
        Worker(repository, harness, mcp_service=service).run_once()
    result = repository.get(scope, run.run_id)
    assert result.error_code == "mcp_tool_failed" and result.status == RunStatus.FAILED
    calls = repository.list_tool_calls(scope, run.run_id, 0, 100)
    assert calls[0]["error_code"] == "mcp_tool_failed" and calls[0]["output"] is None
    assert "private-upstream-message" not in caplog.text


@pytest.mark.parametrize("reason", ["cancel", "expired", "revoked"])
def test_inflight_remote_read_cannot_publish_after_execution_is_lost(
    repository, scope, clock, people, reason
):
    store, members, _ = people
    owner = members["alice"] if reason == "revoked" else scope
    repo = (
        RunRepository(repository.engine, clock, auth_mode="database")
        if reason == "revoked"
        else repository
    )
    remote = Remote()
    service = make_service(owner, remote)
    tool = service.catalog(owner, "products")[0]
    gateway = model_settings()
    run = repo.create(
        owner,
        "task",
        execution_config=ExecutionSnapshot(
            model=gateway.snapshot(),
            tools=(),
            mcp_tools=(tool,),
            authorization_mode="database" if reason == "revoked" else "development",
        ),
    )

    async def lose_execution():
        if reason == "cancel":
            repo.cancel(owner, run.run_id)
        elif reason == "expired":
            clock.advance(61)
        else:
            store.set_member(scope, "alice", display_name="Alice", role="viewer")
        await anyio.sleep(0.2)

    remote.call_hook = lose_execution
    requests = []

    def model(request):
        requests.append(request)
        return httpx.Response(
            200, json=completion(None, [tool_call(name=tool.name, arguments='{"query":"A"}')])
        )

    with configured_harness(gateway, transport=httpx.MockTransport(model)) as harness:
        Worker(repo, harness, mcp_service=service).run_once()
    assert len(requests) == 1 and len(remote.calls()) == 1
    assert repo.get(owner, run.run_id).result is None
    assert not any(e.event_type == "tool_completed" for e in repo.list_events(owner, run.run_id))


def test_deadline_and_cancellation_stop_waiting_for_remote(scope):
    remote = Remote()
    service = make_service(scope, remote, timeout_seconds=0.2)
    tool = service.catalog(scope, "products")[0]

    async def slow():
        await anyio.sleep(10)

    remote.call_hook = slow
    with pytest.raises(McpFailure, match="mcp_timeout"):
        service.call(scope, tool, {"query": "A"}, lambda: True)
    with pytest.raises(McpCancelled):
        service.call(scope, tool, {"query": "A"}, lambda: False)
    assert len(remote.calls()) == 1


@pytest.mark.parametrize("kind", ["redirect", "invalid_json", "oversize", "gzip"])
def test_http_transport_limits_and_private_logs(scope, caplog, kind):
    remote = Remote()
    service = make_service(scope, remote)

    async def fail(request):
        if kind == "redirect":
            return httpx2.Response(302, headers={"location": "https://private-target.example"})
        if kind == "invalid_json":
            return httpx2.Response(
                200, text='{"private-input":', headers={"content-type": "application/json"}
            )
        if kind == "gzip":
            return httpx2.Response(200, content=b"", headers={"content-encoding": "gzip"})
        return httpx2.Response(
            200, content=b"x" * 512001, headers={"content-type": "application/json"}
        )

    service.transport_factory = lambda: httpx2.MockTransport(fail)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(McpFailure) as error:
        service.catalog(scope, "products")
    assert "private-input" not in caplog.text + str(error.value)
    assert "private-target" not in caplog.text + str(error.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "file:///private/data"},
        {"url": "https://u:p@host/mcp"},
        {"url": "http://internal/mcp"},
        {"url": "https://host/mcp?token=x"},
        {"token_env": "HOME"},
        {"users": ["*"]},
        {"read_tools": {"search": True}},
    ],
)
def test_invalid_deployment_config_is_rejected(scope, fields):
    base = make_service(scope, Remote()).settings.servers[0].model_dump()
    with pytest.raises(ValueError):
        McpServer.model_validate({**base, **fields})


def test_no_config_is_offline_and_config_errors_hide_values(scope, tmp_path, monkeypatch):
    assert McpService().servers(scope) == []
    path = tmp_path / "bad.json"
    path.write_text('{"private-configuration": "secret"}')
    monkeypatch.setenv("AGENTFORGE_MCP_SERVERS_FILE", str(path))
    with pytest.raises(ValueError) as error:
        McpSettings.from_env()
    assert "secret" not in str(error.value) and str(path) not in str(error.value)


def test_schema_references_and_patterns_are_not_resolved():
    for value in (
        {"type": "object", "$ref": "https://private/schema"},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}},
        {"type": "object", "properties": {"x": {"type": "broken"}}},
    ):
        with pytest.raises(ValueError):
            validate_schema(value)


def test_original_rpc_chat_can_combine_skill_and_mcp(settings, repository, scope, headers):
    from tests.test_orpc import rpc

    service = make_service(scope, Remote())
    snapshot = service.catalog(scope, "products")[0]
    gateway = model_settings()
    with TestClient(
        create_app(settings, repository, model_settings=gateway, mcp_service=service)
    ) as client:
        agent = client.post(
            "/api/v1/agents",
            headers=headers,
            json={
                "name": "MCP bot",
                "tools": [],
                "mcp_tools": [selection(snapshot)],
                "skills": ["builtin:document_summary@1"],
            },
        ).json()
        updated = rpc(
            client,
            headers,
            "bots.update",
            {"botId": agent["agent_id"], "instructions": "Use Chinese"},
        )
        assert updated.status_code == 200
        receipt = rpc(
            client,
            headers,
            "threads.send",
            {"botId": agent["agent_id"], "text": "/资料摘要\nFind A"},
        )
        assert receipt.status_code == 200, receipt.text
        rid = receipt.json()["json"]["runId"]
        saved = repository.get(scope, rid)
        assert saved.execution_config.mcp_tools == (snapshot,)
        assert saved.execution_config.requested_skill == "builtin:document_summary@1"
        rounds = []

        def model(request):
            body = json.loads(request.content)
            rounds.append(body)
            assert {t["function"]["name"] for t in body["tools"]} == {snapshot.name, "skill_read"}
            if len(rounds) == 1:
                return httpx.Response(
                    200,
                    json=completion(
                        None,
                        [
                            tool_call(
                                "skill",
                                "skill_read",
                                json.dumps({"reference": "builtin:document_summary@1"}),
                            )
                        ],
                    ),
                )
            if len(rounds) == 2:
                assert "资料摘要" in body["messages"][-1]["content"]
                return httpx.Response(
                    200,
                    json=completion(
                        None, [tool_call("mcp", snapshot.name, json.dumps({"query": "A"}))]
                    ),
                )
            return httpx.Response(200, json=completion("产品 A 的值为 42"))

        with configured_harness(gateway, transport=httpx.MockTransport(model)) as harness:
            Worker(repository, harness, mcp_service=service).run_once()
        assert len(rounds) == 3
        assert repository.get(scope, rid).result == "产品 A 的值为 42"
        page = rpc(client, headers, "threads.get", {"botId": agent["agent_id"]})
        assert page.status_code == 200
        assert "产品 A 的值为 42" in json.dumps(page.json(), ensure_ascii=False)


def test_shared_agent_does_not_share_mcp_grant(settings, repository, people):
    store, scopes, credentials = people
    remote = Remote()
    service = make_service(scopes["alice"], remote)
    tool = service.catalog(scopes["alice"], "products")[0]
    gateway = model_settings()
    repo = RunRepository(repository.engine, repository.clock, auth_mode="database")
    headers = {n: {"Authorization": "Bearer " + c["token"]} for n, c in credentials.items()}
    with TestClient(
        create_app(
            replace(settings, auth_mode="database"),
            repo,
            model_settings=gateway,
            mcp_service=service,
        )
    ) as client:
        assert (
            client.get("/api/v1/mcp/servers/products/tools", headers=headers["bob"]).status_code
            == 404
        )
        count = len(remote.requests)
        assert (
            client.post(
                "/api/v1/agents",
                headers=headers["viewer"],
                json={"name": "blocked", "mcp_tools": [selection(tool)]},
            ).status_code
            == 403
        )
        assert len(remote.requests) == count
        agent = client.post(
            "/api/v1/agents",
            headers=headers["alice"],
            json={"name": "Shared reader", "tools": [], "mcp_tools": [selection(tool)]},
        ).json()
        assert (
            client.put(
                f"/api/v1/sharing/agent/{agent['agent_id']}",
                headers=headers["alice"],
                json={"visibility": "space"},
            ).status_code
            == 200
        )
        thread = client.post(
            "/api/v1/threads", headers=headers["bob"], json={"agent_id": agent["agent_id"]}
        ).json()
        run = client.post(
            "/api/v1/runs",
            headers=headers["bob"],
            json={"thread_id": thread["thread_id"], "prompt": "try"},
        ).json()
        with configured_harness(
            gateway, transport=httpx.MockTransport(lambda _: pytest.fail("Model must not run"))
        ) as harness:
            Worker(repo, harness, mcp_service=service).run_once()
        assert repo.get(scopes["bob"], run["run_id"]).error_code == "mcp_not_allowed"
        assert not remote.calls()


def test_rpc_failure_is_not_replayed_by_sdk(scope):
    remote = Remote()
    service = make_service(scope, remote)
    tool = service.catalog(scope, "products")[0]

    async def fail(request):
        data = json.loads(request.content) if request.method == "POST" else {}
        if data.get("method") == "tools/call":
            remote.requests.append((data, dict(request.headers)))
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "error": {"code": -32602, "message": "private-result-unknown"},
                },
            )
        return await remote.handle(request)

    service.transport_factory = lambda: httpx2.MockTransport(fail)
    with pytest.raises(McpFailure):
        service.call(scope, tool, {"query": "A"}, lambda: True)
    assert len(remote.calls()) == 1


def test_shipped_demo_configuration_and_sdk_server():
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    settings = McpSettings.model_validate_json(
        (root / "infra/mcp/servers.example.json").read_text()
    )
    server = runpy.run_path(str(root / "examples/mcp_catalog.py"))["server"]
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    config = settings.servers[0]
    service = McpService(settings, transport_factory=lambda: httpx2.ASGITransport(app=app))

    async def exercise():
        async with app.router.lifespan_context(app):
            tool = (await service.exchange(config, None, None, lambda: True))[0]
            result = await service.exchange(config, tool, {"query": "DEMO-001"}, lambda: True)
            assert result["structured"]["name"] == "示例产品"

    anyio.run(exercise)


def test_native_model_reset_cannot_turn_bound_mcp_into_mock(settings, repository, scope, headers):
    from agentforge_runtime.config import ModelSettings

    from tests.test_orpc import rpc

    remote = Remote()
    service = make_service(scope, remote)
    tool = service.catalog(scope, "products")[0]
    with TestClient(
        create_app(settings, repository, model_settings=model_settings(), mcp_service=service)
    ) as client:
        agent = client.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": "reader", "tools": [], "mcp_tools": [selection(tool)]},
        ).json()
    with TestClient(
        create_app(settings, repository, model_settings=ModelSettings(), mcp_service=service)
    ) as client:
        result = rpc(
            client,
            headers,
            "bots.update",
            {"botId": agent["agent_id"], "modelProvider": None, "modelId": None},
        )
        assert result.status_code == 422
        assert client.get(f"/api/v1/agents/{agent['agent_id']}", headers=headers).json() == agent
