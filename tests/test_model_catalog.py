import json
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa
from agentforge_api.app import create_app
from agentforge_core.execution import ModelConfig
from agentforge_core.models import Scope
from agentforge_runtime.chat_harness import configured_harness
from agentforge_runtime.config import ModelSettings
from agentforge_storage.model_preferences import ModelPreferences
from agentforge_storage.passwords import PasswordLogin
from agentforge_storage.schema import rpc_model_preferences
from agentforge_worker.worker import Worker
from fastapi.testclient import TestClient

from tests.model_support import completion, model_settings, tool_call
from tests.test_identity import people as _people
from tests.test_orpc import create_bot, rpc
from tests.test_orpc_interop import run_bridge

people = _people


def gateway_settings():
    return model_settings(
        model="deepseek-v4-pro",
        allowed_models=("test-second-model",),
        api_style="deepseek",
        base_url="https://api.deepseek.com",
    )


def test_native_models_catalog_default_and_bot_selection(settings, repository, headers, scope):
    gateway = gateway_settings()
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        catalog = rpc(client, headers, "models.list").json()["json"]
        assert [entry["id"] for entry in catalog] == list(gateway.model_ids)
        creds = rpc(client, headers, "models.credentials").json()["json"]
        assert creds[0]["modelId"] == "deepseek-v4-pro"
        assert not creds[0]["hasKey"]  # API exposes no personal key and makes no liveness claim.
        assert gateway.api_key not in json.dumps(creds)
        assert gateway.base_url not in json.dumps(creds)
        bot = create_bot(client, headers)
        sent = rpc(client, headers, "threads.send", {"botId": bot["id"], "text": "queued"}).json()[
            "json"
        ]
        assert (
            rpc(
                client,
                headers,
                "models.setDefault",
                {
                    "provider": gateway.provider,
                    "modelId": "test-second-model",
                    "thinkingLevel": None,
                },
            ).status_code
            == 200
        )
        assert rpc(client, headers, "me").json()["json"]["defaultModel"] == "test-second-model"
        assert create_bot(client, headers)["modelId"] == "test-second-model"
        updated = rpc(
            client,
            headers,
            "bots.update",
            {
                "botId": bot["id"],
                "modelProvider": None,
                "modelId": None,
            },
        ).json()["json"]
        assert updated["modelId"] == "test-second-model"
        assert (
            repository.get(scope, sent["runId"]).execution_config.model.model == "deepseek-v4-pro"
        )
        for body in [
            {"provider": "unknown", "modelId": "deepseek-v4-pro"},
            {"provider": gateway.provider, "modelId": "unknown"},
            {"provider": gateway.provider, "modelId": "deepseek-v4-pro", "thinkingLevel": "high"},
        ]:
            assert rpc(client, headers, "models.setDefault", body).status_code == 422
        assert (
            rpc(
                client,
                headers,
                "bots.update",
                {
                    "botId": bot["id"],
                    "modelProvider": gateway.provider,
                    "modelId": "unknown",
                },
            ).status_code
            == 422
        )
        assert rpc(client, headers, "models.connect", {"apiKey": "not-accepted"}).status_code == 422
    # Recreating the API preserves the preference; no in-memory process default.
    with TestClient(create_app(settings, repository, model_settings=gateway)) as client:
        assert rpc(client, headers, "me").json()["json"]["defaultModel"] == "test-second-model"
    preferences = ModelPreferences(repository, gateway)
    for other in [
        replace(scope, user_id="other"),
        replace(scope, space_id="other"),
        Scope("other", scope.space_id, scope.user_id),
    ]:
        assert preferences.default(other).model == "deepseek-v4-pro"
    for changed in [
        replace(gateway, allowed_models=()),
        replace(gateway, base_url="https://new.example"),
    ]:
        assert ModelPreferences(repository, changed).default(scope).model == "deepseek-v4-pro"


def test_model_preferences_enforce_membership_and_viewer_permissions(settings, repository, people):
    identities, scopes, tokens = people
    with TestClient(
        create_app(
            replace(settings, auth_mode="database"), repository, model_settings=gateway_settings()
        )
    ) as client:
        headers = {
            name: {"Authorization": "Bearer " + token["token"]} for name, token in tokens.items()
        }
        body = {"provider": "chat_completions", "modelId": "test-second-model"}
        assert rpc(client, headers["alice"], "models.setDefault", body).status_code == 200
        assert rpc(client, headers["bob"], "me").json()["json"]["defaultModel"] == "deepseek-v4-pro"
        assert rpc(client, headers["viewer"], "models.setDefault", body).status_code == 403
        assert rpc(client, {}, "models.list").status_code == 403
        identities.set_member(
            scopes["admin"], "alice", display_name="alice", role="member", active=False
        )
        assert rpc(client, headers["alice"], "models.setDefault", body).status_code == 401
        with repository.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(rpc_model_preferences)
                ).scalar_one()
                == 1
            )


def test_deepseek_wire_format_tool_loop_and_snapshot_protocol():
    gateway = gateway_settings()
    requests = []

    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        assert body["max_tokens"] == 2048
        assert "max_completion_tokens" not in body
        assert body["thinking"] == {"type": "disabled"}
        if len(requests) == 1:
            return httpx.Response(200, json=completion(None, [tool_call()]))
        assert body["messages"][-2]["content"] is None
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["content"].endswith("资料摘要")
        return httpx.Response(200, json=completion())

    with configured_harness(gateway, transport=httpx.MockTransport(respond)) as harness:
        assert harness.execute("task", lambda _: True) == "最终回答"
    assert gateway.for_snapshot(gateway.snapshot()).api_style == "deepseek"
    old = ModelConfig.model_validate(
        {key: value for key, value in gateway.snapshot().model_dump().items() if key != "api_style"}
    )
    assert old.api_style == "openai"
    assert gateway.for_snapshot(old).api_style == "openai"
    # Previously submitted model choices remain pinned even if removed from the catalog.
    assert (
        replace(gateway, model="different", allowed_models=())
        .for_snapshot(gateway.snapshot())
        .model
        == gateway.model
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"api_style": "bad"},
        {"model": "bad\nmodel"},
        {"allowed_models": ("",)},
        {"allowed_models": (" bad",)},
        {"allowed_models": tuple(str(i) for i in range(100))},
    ],
)
def test_invalid_catalog_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        model_settings(**changes)


def test_model_catalog_env_does_not_load_worker_key(monkeypatch):
    for key, value in {
        "PROVIDER": "chat_completions",
        "BASE_URL": "https://api.deepseek.com",
        "NAME": "deepseek-v4-pro",
        "API_STYLE": "deepseek",
        "API_KEY": "test-only-secret",
        "ALLOWED_MODELS": "test-second-model, deepseek-v4-pro",
    }.items():
        monkeypatch.setenv("AGENTFORGE_MODEL_" + key, value)
    settings = ModelSettings.from_env(require_credentials=False)
    assert not settings.api_key
    assert settings.model_ids == ("deepseek-v4-pro", "test-second-model")
    assert settings.select("chat_completions", "test-second-model").model_ids == (
        "test-second-model",
        "deepseek-v4-pro",
    )


def test_native_login_model_choice_multiturn_tools_and_error(settings, repository, people):
    gateway = gateway_settings()
    PasswordLogin(people[0]).set_password(
        people[1]["alice"], "native@example.test", "test-native-password-only"
    )
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert body["model"] == ("deepseek-v4-pro" if len(requests) <= 2 else "test-second-model")
        if len(requests) == 1:
            return httpx.Response(200, json=completion(None, [tool_call()]))
        if len(requests) == 2:
            assert body["messages"][-1]["role"] == "tool"
            return httpx.Response(200, json=completion("First tool result"))
        if len(requests) == 3:
            assert {"role": "assistant", "content": "First tool result"} in body["messages"]
            assert {"role": "user", "content": "First gateway question"} in body["messages"]
            return httpx.Response(200, json=completion("Second context result"))
        return httpx.Response(401, text="private-upstream-details")

    worker = Worker(
        repository,
        harness_factory=lambda snapshot: configured_harness(
            gateway.for_snapshot(snapshot.model),
            transport=httpx.MockTransport(respond),
        ),
    )
    with TestClient(
        create_app(
            replace(settings, auth_mode="database", web_origin="http://testserver"),
            repository,
            model_settings=replace(gateway, api_key="", require_credentials=False),
        )
    ) as client:
        run_bridge(client, repository, browser=True, native=True, model_chat=True, worker=worker)
    assert len(requests) == 4
