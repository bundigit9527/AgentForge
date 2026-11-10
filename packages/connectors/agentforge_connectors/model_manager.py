"""Model management orchestration shared by API and Python Worker."""

import os
from dataclasses import replace
from uuid import uuid4

from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.model_limits import connect_window, connection_window
from agentforge_core.model_management import ModelManagementError
from agentforge_runtime.chat_model import ChatCompletionsModel
from agentforge_runtime.config import ModelSettings
from agentforge_runtime.errors import RuntimeFailure
from agentforge_storage.model_connections import ModelConnections
from agentforge_storage.model_preferences import ModelPreferences

from agentforge_connectors.model_endpoint import ModelEndpointTransport, discover, endpoint
from agentforge_connectors.model_secrets import cipher, seal, unseal

DEEPSEEK = "https://api.deepseek.com"


class ModelManager:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.repo = ModelConnections(store)
        self.legacy = ModelPreferences(store, settings)

    @property
    def enabled(self):
        return bool(os.environ.get("AGENTFORGE_MODEL_ENCRYPTION_KEY"))

    def require(self, scope):
        cipher()
        with self.repo.connection(scope, write=True):
            pass

    def configured(self, row, *, key="", require_key=False, model=None):
        config = row["configuration"]
        model = model or config["modelId"]
        window = connection_window(row["provider"], config, model)
        budget = window - config["maxTokens"]
        return ModelSettings(
            provider="chat_completions",
            base_url=config["baseUrl"],
            api_key=key,
            model=model,
            allowed_models=tuple(config["models"]),
            api_style=config["apiStyle"],
            stream=self.settings.stream,
            context_policy="trim_history",
            context_max_chars=max(4000, budget),
            context_max_bytes=budget,
            context_window_tokens=window,
            compact_tool_results=True,
            timeout_seconds=self.settings.timeout_seconds,
            max_rounds=self.settings.max_rounds,
            max_completion_tokens=config["maxTokens"],
            require_credentials=require_key,
            connection_id=row["connection_id"],
            connection_revision=row["revision"],
            catalog_provider=row["provider"],
        )

    def select(self, scope, provider, model, connection=None):
        if provider == self.settings.provider:
            return self.settings.select(provider, model)
        row = self.repo.get(scope, provider, connection)
        if row is None or model not in row["configuration"]["models"]:
            raise ModelManagementError("Connect this model for your account first")
        return self.configured(row, model=model)

    def default(self, scope, *, connection=None):
        row = self.repo.choice(scope, connection)
        if row:
            try:
                return self.select(scope, row["provider"], row["model_id"], connection)
            except (ModelManagementError, ValueError):
                pass
        return self.legacy.default(scope, connection=connection)

    def set_default(self, scope, provider, model):
        if not self.enabled or self.store.authorization.mode != "database":
            return self.legacy.set_default(scope, provider, model)
        with self.repo.connection(scope, write=True) as conn:
            self.select(scope, provider, model, conn)
            self.repo.select(scope, provider, model, conn)
        return {"ok": True}

    def connect(self, scope, body):
        self.require(scope)
        old = self.repo.get(scope, body.provider, include_inactive=True)
        base = endpoint(DEEPSEEK if body.provider == "deepseek" else body.baseUrl)
        previous = old["configuration"] if old and old["active"] else {}
        if body.apiKey is None:
            if old and old["active"]:
                if previous["baseUrl"] != base:
                    raise ModelManagementError("Enter a new API key when changing the endpoint")
                key = unseal(old)
            elif body.provider == "openai-compatible":
                key = (
                    ""  # Local/authless gateways are an explicit connection, never a borrowed key.
                )
            else:
                raise ModelManagementError("Provide the provider API key")
        else:
            key = body.apiKey
        maximum = body.maxTokens if body.maxTokens is not None else previous.get("maxTokens", 4096)
        window, window_source = connect_window(body, previous)
        if window - maximum < 1024:
            raise ModelManagementError("Maximum output must leave at least 1024 tokens for input")
        models = (
            list(dict.fromkeys([*previous.get("models", []), body.modelId]))
            if previous.get("baseUrl") == base
            else [body.modelId]
        )
        if len(models) > 100:
            raise ModelManagementError("At most 100 models per connection")
        row = dict(
            connection_id=old["connection_id"] if old else str(uuid4()),
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            provider=body.provider,
            revision=old["revision"] + 1 if old else 1,
            active=True,
            configuration=dict(
                baseUrl=base,
                modelId=body.modelId,
                models=models,
                label=body.label or body.provider,
                hasKey=bool(key),
                maxTokens=maximum,
                contextWindow=window,
                contextWindowSource=window_source,
                apiStyle="deepseek" if base in {DEEPSEEK, DEEPSEEK + "/v1"} else "compatible",
            ),
        )
        encrypted = seal(row, key)
        settings = replace(
            self.configured(row, key=key),
            stream=False,
            max_completion_tokens=32,
            timeout_seconds=15,
        )
        try:
            model = ChatCompletionsModel(settings, transport=ModelEndpointTransport(base))
            try:
                model.complete([{"role": "user", "content": "Reply with OK."}], [])
            finally:
                model.close()
        except (RuntimeFailure, ModelManagementError):
            raise ModelManagementError(
                "Model connection test failed; check address, key and model", 422
            ) from None
        row["encrypted_key"] = encrypted
        self.repo.save(scope, row, old["revision"] if old else None)
        return self.dto(scope, row)

    def dto(self, scope, row):
        choice = self.repo.choice(scope)
        config = row["configuration"]
        is_default = bool(choice and choice["provider"] == row["provider"])
        return dict(
            id=row["connection_id"],
            provider=row["provider"],
            label=config["label"],
            hasKey=config["hasKey"],
            isDefault=is_default,
            baseUrl=config["baseUrl"],
            modelId=choice["model_id"] if is_default else config["modelId"],
            reasoning=False,
            thinkingLevel=None,
            thinkingLevels=[],
            supportsImages=False,
            maxTokens=config["maxTokens"],
            contextWindow=connection_window(
                row["provider"], config, choice["model_id"] if is_default else config["modelId"]
            ),
            authKind="openai_compatible" if row["provider"] == "openai-compatible" else "api_key",
        )

    def catalog(self, scope):
        rows = self.repo.rows(scope)
        entries = []
        for provider, title, fallback in [
            ("deepseek", "DeepSeek", "deepseek-v4-pro"),
            ("openai-compatible", "OpenAI Compatible", "custom"),
        ]:
            row = next((r for r in rows if r["provider"] == provider), None)
            for model in row["configuration"]["models"] if row else [fallback]:
                entries.append(
                    dict(
                        provider=provider,
                        providerName=title,
                        id=model,
                        label=model,
                        billing="Billed by your model provider",
                        auth="api-key",
                        reasoning=False,
                        thinkingLevels=[],
                        catalogProbe=provider == "deepseek",
                        placeholder=row is None and provider == "openai-compatible",
                    )
                )
        return entries

    def backup_choices(self, scope, connection=None):
        return self.repo.backups(scope, connection)

    def set_backups(self, scope, choices):
        if len(choices) > 10 or len({(c.provider, c.modelId) for c in choices}) != len(choices):
            raise ModelManagementError("Choose at most 10 distinct backup models")
        with self.repo.connection(scope, write=True) as conn:
            for choice in choices:
                self.select(scope, choice.provider, choice.modelId, conn)
            self.repo.set_backups(scope, [c.model_dump() for c in choices], conn)
        return {"ok": True}

    def prepare_run(self, scope, snapshot, connection):
        primary = snapshot.model
        if primary.connection_id:
            primary = self.select(
                scope, primary.catalog_provider, primary.model, connection
            ).snapshot()
        backups = [
            self.select(scope, c["provider"], c["modelId"], connection).snapshot()
            for c in self.repo.backups(scope, connection)
        ]
        return ExecutionSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "model": primary,
                "model_backups": tuple(
                    m
                    for m in backups
                    if (m.catalog_provider or m.provider, m.model)
                    != (primary.catalog_provider or primary.provider, primary.model)
                ),
            }
        )

    def resolve(self, scope, snapshot):
        if not snapshot.connection_id:
            with self.repo.connection(scope):
                return self.settings.for_snapshot(snapshot)
        row = self.repo.get(scope, snapshot.catalog_provider)
        if not row or (row["connection_id"], row["revision"]) != (
            snapshot.connection_id,
            snapshot.connection_revision,
        ):
            raise ModelManagementError(
                "Model connection changed or was disconnected; submit a new run"
            )
        if (
            snapshot.model not in row["configuration"]["models"]
            or snapshot.api_style != row["configuration"]["apiStyle"]
        ):
            raise ModelManagementError("Model is not enabled on this connection")
        return self.configured(row, key=unseal(row), require_key=False).for_snapshot(snapshot)

    def probe(self, scope, base_url, key):
        self.require(scope)
        return {"models": discover(base_url, key or "")}
