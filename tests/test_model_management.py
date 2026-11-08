"""Original model-management contract, isolation, secret lifecycle and Worker routing."""

import base64
import json
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_connectors import model_endpoint, model_execution, model_manager
from agentforge_connectors.model_manager import ModelManager
from agentforge_connectors.model_secrets import unseal
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.model_management import Choice, ModelManagementError
from agentforge_core.models import RunConflict
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_storage.repository import RunRepository
from agentforge_storage.schema import model_connections
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, tool_call
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc

people = _people
KEY = "personal-test-key-do-not-use"
BODY = {
    "provider": "openai-compatible",
    "baseUrl": "https://model.example/v1",
    "modelId": "alpha",
    "apiKey": KEY,
}


class Upstream:
    def __init__(self):
        self.requests = []
        self.action = lambda _: httpx.Response(200, json=completion("connected"))

    def handle(self, request):
        self.requests.append(request)
        return self.action(request)


@pytest.fixture
def managed(settings, repository, people, monkeypatch):
    monkeypatch.setenv(
        "AGENTFORGE_MODEL_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"t" * 32).decode().rstrip("=")
    )
    upstream = Upstream()
    for module in (model_manager, model_endpoint, model_execution):
        monkeypatch.setattr(
            module, "ModelEndpointTransport", lambda _: httpx.MockTransport(upstream.handle)
        )
    with TestClient(
        create_app(
            replace(settings, auth_mode="database", web_origin="http://testserver"),
            repository,
            model_settings=ModelSettings(),
        )
    ) as client:
        manager = ModelManager(
            RunRepository(repository.engine, repository.clock, auth_mode="database"),
            ModelSettings(),
        )
        headers = {name: {"Authorization": "Bearer " + t["token"]} for name, t in people[2].items()}
        yield client, headers, manager, people[1], upstream


def connect(managed, name="alice", **values):
    client, headers, _, _, _ = managed
    response = rpc(client, headers[name], "models.connect", {**BODY, **values})
    assert response.status_code == 200, response.text
    assert KEY not in response.text
    return response.json()["json"]


def send(managed, bot=None):
    client, headers, _, _, _ = managed
    bot = bot or create_bot(client, headers["alice"])
    reply = rpc(
        client, headers["alice"], "threads.send", {"botId": bot["id"], "text": "Report the result"}
    )
    assert reply.status_code == 200, reply.text
    return bot, reply.json()["json"]["runId"]


def test_connect_discover_default_bot_and_backup_contract(managed, repository):
    client, headers, manager, scopes, upstream = managed
    catalog = rpc(client, headers["alice"], "models.list").json()["json"]
    assert {m["provider"] for m in catalog} == {"deepseek", "openai-compatible"}
    upstream.action = lambda _: httpx.Response(
        200, json={"data": [{"id": "alpha"}, {"id": "beta"}]}
    )
    assert rpc(
        client,
        headers["alice"],
        "models.probeOpenAiCompatible",
        {"baseUrl": BODY["baseUrl"], "apiKey": KEY},
    ).json()["json"] == {"models": ["alpha", "beta"]}
    upstream.action = lambda _: httpx.Response(200, json=completion())
    first = connect(managed)
    assert first["hasKey"] and first["isDefault"] and first["authKind"] == "openai_compatible"
    first_request = json.loads(upstream.requests[-1].content)
    assert first_request["max_tokens"] == 32
    assert "max_completion_tokens" not in first_request
    bot, queued = send(managed)
    assert bot["modelProvider"] == "openai-compatible"
    connect(managed, modelId="beta", apiKey=None)
    assert rpc(client, headers["alice"], "me").json()["json"]["defaultModel"] == "beta"
    assert (
        rpc(
            client,
            headers["alice"],
            "models.setDefault",
            {"provider": "openai-compatible", "modelId": "alpha", "thinkingLevel": "off"},
        ).status_code
        == 200
    )
    choices = [{"provider": "openai-compatible", "modelId": "beta"}]
    assert rpc(client, headers["alice"], "models.setBackups", choices).status_code == 200
    assert rpc(client, headers["alice"], "models.backups").json()["json"] == choices
    assert rpc(client, headers["bob"], "models.backups").json()["json"] == []
    assert rpc(client, headers["alice"], "models.setBackups", choices * 2).status_code == 422
    updated = rpc(
        client,
        headers["alice"],
        "bots.update",
        {"botId": bot["id"], "modelProvider": "openai-compatible", "modelId": "beta"},
    )
    assert updated.status_code == 200, updated.text
    old = repository.get(scopes["alice"], queued).execution_config
    assert old.model.model == "alpha" and old.model.connection_revision == 1
    _, next_run = send(managed)
    snapshot = repository.get(scopes["alice"], next_run).execution_config
    assert snapshot.model.model == "alpha" and snapshot.model.connection_revision == 2
    assert snapshot.model_backups[0].model == "beta"
    assert KEY not in snapshot.model_dump_json()
    row = manager.repo.get(scopes["alice"], "openai-compatible")
    assert KEY.encode() not in row["encrypted_key"]
    assert KEY not in json.dumps(row["configuration"])
    assert unseal(row) == KEY


def test_secrets_ownership_revocation_and_tamper(managed, repository, monkeypatch):
    client, headers, manager, scopes, _ = managed
    connect(managed)
    snapshot = manager.default(scopes["alice"]).snapshot()
    assert rpc(client, headers["bob"], "models.credentials").json()["json"] == []
    for who in ("bob", "viewer"):
        response = rpc(
            client,
            headers[who],
            "models.setDefault",
            {"provider": "openai-compatible", "modelId": "alpha"},
        )
        assert response.status_code in {403, 422}
    for method, body in (
        ("models.connect", BODY),
        ("models.probeOpenAiCompatible", {"baseUrl": BODY["baseUrl"]}),
        ("models.setBackups", []),
        ("models.disconnect", {"provider": "openai-compatible"}),
    ):
        assert rpc(client, headers["viewer"], method, body).status_code == 403
    with pytest.raises(ModelManagementError):
        manager.resolve(scopes["bob"], snapshot)
    row = manager.repo.get(scopes["alice"], "openai-compatible")
    with pytest.raises(ModelManagementError):
        unseal({**row, "user_id": "bob"})
    monkeypatch.setenv("AGENTFORGE_MODEL_ENCRYPTION_KEY", base64.b64encode(b"x" * 32).decode())
    with pytest.raises(ModelManagementError):
        manager.resolve(scopes["alice"], snapshot)
    # Disconnect does not require decryption, so a lost key cannot trap a user.
    assert (
        rpc(
            client, headers["alice"], "models.disconnect", {"provider": "openai-compatible"}
        ).status_code
        == 200
    )
    with pytest.raises(ModelManagementError):
        manager.resolve(scopes["alice"], snapshot)
    with repository.engine.connect() as conn:
        assert conn.execute(sa.select(model_connections.c.encrypted_key)).scalar_one() == b""


def test_connection_failures_keep_old_key_and_selection(managed):
    client, headers, manager, scopes, upstream = managed
    connect(managed)
    row = manager.repo.get(scopes["alice"], "openai-compatible")
    assert (
        rpc(
            client,
            headers["alice"],
            "models.connect",
            {**BODY, "baseUrl": "https://different.example", "apiKey": None},
        ).status_code
        == 422
    )
    upstream.action = lambda _: httpx.Response(401, text=KEY)
    failure = rpc(client, headers["alice"], "models.connect", {**BODY, "apiKey": "replacement-key"})
    assert failure.status_code == 422 and KEY not in failure.text
    assert manager.repo.get(scopes["alice"], "openai-compatible") == row
    with pytest.raises(RunConflict):
        manager.repo.save(scopes["alice"], row, 0)
    for values in (
        {"supportsImages": True},
        {"reasoning": True},
        {"thinkingLevel": "high"},
        {"contextWindow": 4000, "maxTokens": 4096},
    ):
        assert (
            rpc(client, headers["alice"], "models.connect", {**BODY, **values}).status_code == 422
        )


def test_authless_gateway_and_deepseek_protocol(managed, repository):
    _, _, manager, scopes, upstream = managed
    result = connect(managed, apiKey=None)
    assert not result["hasKey"]
    assert "authorization" not in upstream.requests[-1].headers
    _, run_id = send(managed)
    assert Worker(repository, model_manager=manager).run_once()
    assert repository.get(scopes["alice"], run_id).status.value == "succeeded"
    assert "authorization" not in upstream.requests[-1].headers
    connect(managed, provider="deepseek", baseUrl=None, modelId="deepseek-v4-pro")
    payload = json.loads(upstream.requests[-1].content)
    assert payload["thinking"] == {"type": "disabled"} and payload["max_tokens"] == 32
    assert upstream.requests[-1].url == "https://api.deepseek.com/chat/completions"


def test_worker_backup_continues_same_tool_loop(managed, repository):
    _, _, manager, scopes, upstream = managed
    connect(managed)
    connect(managed, modelId="beta", apiKey=None)
    manager.set_default(scopes["alice"], "openai-compatible", "alpha")
    manager.set_backups(scopes["alice"], [Choice(provider="openai-compatible", modelId="beta")])
    _, run_id = send(managed)
    upstream.requests.clear()

    def answer(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer " + KEY
        if body["model"] == "alpha":
            return httpx.Response(429)
        if not any(m["role"] == "tool" for m in body["messages"]):
            return httpx.Response(200, json=completion(None, [tool_call()]))
        return httpx.Response(200, json=completion("Backup completed the report"))

    upstream.action = answer
    assert Worker(repository, model_manager=manager).run_once()
    run = repository.get(scopes["alice"], run_id)
    assert run.status.value == "succeeded", run.error_code
    assert [json.loads(r.content)["model"] for r in upstream.requests] == ["alpha", "beta", "beta"]
    # The backup sees the result from the single executed tool call.
    assert (
        len(
            [
                m
                for m in json.loads(upstream.requests[-1].content)["messages"]
                if m["role"] == "tool"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_fallback_after_pre_output_request_failure(managed, status):
    _, _, manager, scopes, upstream = managed
    connect(managed)
    connect(managed, modelId="beta", apiKey=None)
    primary = manager.select(scopes["alice"], "openai-compatible", "alpha").snapshot()
    backup = manager.select(scopes["alice"], "openai-compatible", "beta").snapshot()
    upstream.requests.clear()
    upstream.action = lambda r: (
        httpx.Response(status)
        if json.loads(r.content)["model"] == "alpha"
        else httpx.Response(200, json=completion())
    )
    routed = model_execution.RoutedModel(
        manager,
        scopes["alice"],
        ExecutionSnapshot(model=primary, model_backups=(backup,)),
        lambda: True,
    )
    routed.complete([{"role": "user", "content": "hello"}], [])
    assert routed.name == "beta" and len(upstream.requests) == 2


def test_partial_stream_cancel_and_revocation_never_fall_back(managed):
    _, _, manager, scopes, upstream = managed
    connect(managed)
    connect(managed, modelId="beta", apiKey=None)
    primary = (
        manager.select(scopes["alice"], "openai-compatible", "alpha")
        .snapshot()
        .model_copy(update={"stream": True})
    )
    backup = manager.select(scopes["alice"], "openai-compatible", "beta").snapshot()
    snapshot = ExecutionSnapshot(model=primary, model_backups=(backup,))
    upstream.requests.clear()

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("secret upstream error")

    upstream.action = lambda _: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=Broken()
    )
    output = []
    routed = model_execution.RoutedModel(manager, scopes["alice"], snapshot, lambda: True)
    with pytest.raises(RuntimeFailure):
        routed.complete([{"role": "user", "content": "hi"}], [], on_text=output.append)
    assert output == ["partial"] and len(upstream.requests) == 1
    routed = model_execution.RoutedModel(manager, scopes["alice"], snapshot, lambda: False)
    with pytest.raises(ExecutionStopped):
        routed.complete([], [])
    manager.repo.disconnect(scopes["alice"], "openai-compatible")
    with pytest.raises(RuntimeFailure) as exc:
        model_execution.RoutedModel(manager, scopes["alice"], snapshot, lambda: True).complete(
            [], []
        )
    assert exc.value.code.value == "model_configuration_mismatch"
    assert len(upstream.requests) == 1


def test_rotation_new_run_uses_new_revision_and_old_run_fails(managed, repository):
    _, _, manager, scopes, upstream = managed
    connect(managed)
    bot, old = send(managed)
    connect(managed, apiKey="rotated-personal-key")
    upstream.requests.clear()
    Worker(repository, model_manager=manager).run_once()
    assert repository.get(scopes["alice"], old).error_code == "model_configuration_mismatch"
    assert not upstream.requests
    _, fresh = send(managed, bot)
    Worker(repository, model_manager=manager).run_once()
    assert repository.get(scopes["alice"], fresh).status.value == "succeeded"
    assert upstream.requests[-1].headers["authorization"] == "Bearer rotated-personal-key"


def test_original_model_management_client_and_schemas(managed, repository, people):
    from agentforge_storage.passwords import PasswordLogin

    from tests.test_orpc_interop import run_bridge

    client, _, manager, scopes, upstream = managed
    PasswordLogin(people[0]).set_password(
        scopes["alice"], "native@example.test", "test-native-password-only"
    )
    upstream.action = lambda request: (
        httpx.Response(200, json={"data": [{"id": "alpha"}, {"id": "beta"}]})
        if request.method == "GET"
        else httpx.Response(200, json=completion("Personal model chat completed"))
    )
    run_bridge(
        client,
        repository,
        browser=True,
        native=True,
        model_management=True,
        worker=Worker(manager.store, model_manager=manager),
    )


def test_backup_page_scope_preflight_cannot_change_identity(managed):
    client, headers, _, scopes, _ = managed
    current = scopes["alice"].space_id
    # The unmodified ModelBackupsSettings calls rpc.me({context: {spaceId}}).
    reply = rpc(client, headers["alice"], "me", {"context": {"spaceId": current}})
    assert reply.status_code == 200, reply.text
    assert reply.json()["json"]["userId"] == "alice"
    assert reply.json()["json"]["spaceId"] == current
    assert (
        rpc(client, headers["alice"], "me", {"context": {"spaceId": "another-space"}}).status_code
        == 409
    )
    assert (
        rpc(
            client,
            {**headers["alice"], "x-rakazo-space-id": "another-space"},
            "me",
            {"context": {"spaceId": "another-space"}},
        ).status_code
        == 403
    )
    for body in [
        {"context": {"spaceId": current, "userId": "bob"}},
        {"context": {"spaceId": []}},
        {"userId": "bob"},
    ]:
        assert rpc(client, headers["alice"], "me", body).status_code == 422
