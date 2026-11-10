import math
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from urllib.parse import urlsplit

from agentforge_core.execution import ModelConfig


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "mock"
    base_url: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    model: str = ""
    allowed_models: tuple[str, ...] = ()
    api_style: str = "openai"
    connection_id: str | None = None
    connection_revision: int | None = None
    catalog_provider: str | None = None
    context_max_bytes: int | None = None
    compact_tool_results: bool = False
    context_window_tokens: int | None = None
    stream: bool = False
    context_policy: str = "trim_history"
    context_max_chars: int = 100000
    timeout_seconds: float = 30
    max_rounds: int = 8
    max_completion_tokens: int = 2048
    require_credentials: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.context_policy not in {"reject", "trim_history"}:
            raise ValueError("AGENTFORGE_MODEL_CONTEXT_POLICY must be reject or trim_history")
        maximum = 2147483647 if self.compact_tool_results else 200000
        if type(self.context_max_chars) is not int or not 4000 <= self.context_max_chars <= maximum:
            raise ValueError("AGENTFORGE_MODEL_CONTEXT_MAX_CHARS must be between 4000 and 200000")
        if type(self.stream) is not bool:
            raise ValueError("AGENTFORGE_MODEL_STREAM must be true or false")
        if self.api_style not in {"openai", "deepseek", "compatible"}:
            raise ValueError("AGENTFORGE_MODEL_API_STYLE must be openai or deepseek")
        if self.provider == "mock" and self.api_style != "openai":
            raise ValueError("Mock models cannot select a remote API style")
        if (
            not isinstance(self.allowed_models, tuple)
            or len(self.allowed_models) > 100
            or any(
                not isinstance(model, str)
                or not model.strip()
                or len(model) > 128
                or model != model.strip()
                or any(ord(char) < 32 for char in model)
                for model in self.allowed_models
            )
        ):
            raise ValueError("AGENTFORGE_MODEL_ALLOWED_MODELS must contain at most 100 model IDs")
        if len(set((self.model, *self.allowed_models))) > 100:
            raise ValueError("At most 100 selectable models are supported")
        if self.provider not in {"mock", "chat_completions"}:
            raise ValueError("AGENTFORGE_MODEL_PROVIDER must be mock or chat_completions")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("AGENTFORGE_MODEL_TIMEOUT_SECONDS must be in (0, 120]")
        if type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 32:
            raise ValueError("AGENTFORGE_MODEL_MAX_ROUNDS must be between 1 and 32")
        if type(self.max_completion_tokens) is not int or not 1 <= self.max_completion_tokens <= (
            131072 if self.connection_id else 8192
        ):
            raise ValueError("AGENTFORGE_MODEL_MAX_COMPLETION_TOKENS must be between 1 and 8192")
        if self.provider == "chat_completions":
            try:
                url = urlsplit(self.base_url)
                valid = (
                    url.scheme in {"http", "https"}
                    and bool(url.hostname)
                    and not url.username
                    and not url.password
                    and not url.query
                    and not url.fragment
                    and not any(char.isspace() for char in self.base_url)
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "AGENTFORGE_MODEL_BASE_URL must be an HTTP(S) base URL "
                    "without credentials or query"
                )
            if (
                self.require_credentials
                and not (self.catalog_provider == "openai-compatible" and self.api_key == "")
                and (not self.api_key.strip() or any(char.isspace() for char in self.api_key))
            ):
                raise ValueError("AGENTFORGE_MODEL_API_KEY must be a token without whitespace")
            if (
                not self.model.strip()
                or len(self.model) > 128
                or self.model != self.model.strip()
                or any(ord(char) < 32 for char in self.model)
            ):
                raise ValueError("AGENTFORGE_MODEL_NAME must contain 1–128 characters")

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> "ModelSettings":
        try:
            context_chars = int(os.environ.get("AGENTFORGE_MODEL_CONTEXT_MAX_CHARS", "100000"))
            timeout = float(os.environ.get("AGENTFORGE_MODEL_TIMEOUT_SECONDS", "30"))
            rounds = int(os.environ.get("AGENTFORGE_MODEL_MAX_ROUNDS", "8"))
            tokens = int(os.environ.get("AGENTFORGE_MODEL_MAX_COMPLETION_TOKENS", "2048"))
        except ValueError:
            raise ValueError(
                "Invalid numeric model configuration; "
                "check timeout, rounds, token and context limits"
            ) from None
        streaming = os.environ.get("AGENTFORGE_MODEL_STREAM", "false").lower()
        if streaming not in {"true", "false"}:
            raise ValueError("AGENTFORGE_MODEL_STREAM must be true or false")
        return cls(
            stream=streaming == "true",
            compact_tool_results=True,
            context_policy=os.environ.get("AGENTFORGE_MODEL_CONTEXT_POLICY", "trim_history"),
            context_max_chars=context_chars,
            provider=os.environ.get("AGENTFORGE_MODEL_PROVIDER", "mock"),
            base_url=os.environ.get("AGENTFORGE_MODEL_BASE_URL", ""),
            api_key=os.environ.get("AGENTFORGE_MODEL_API_KEY", "") if require_credentials else "",
            model=os.environ.get("AGENTFORGE_MODEL_NAME", ""),
            allowed_models=tuple(
                value.strip()
                for value in os.environ.get("AGENTFORGE_MODEL_ALLOWED_MODELS", "").split(",")
                if value.strip()
            ),
            api_style=os.environ.get("AGENTFORGE_MODEL_API_STYLE", "openai"),
            timeout_seconds=timeout,
            max_rounds=rounds,
            max_completion_tokens=tokens,
            require_credentials=require_credentials,
        )

    @property
    def model_ids(self) -> tuple[str, ...]:
        return (
            tuple(dict.fromkeys((self.model, *self.allowed_models)))
            if self.provider != "mock"
            else ()
        )

    def select(self, provider: str | None, model: str | None) -> "ModelSettings":
        if self.provider == "mock" and provider == "mock" and model in {None, ""}:
            return self
        if provider != self.provider or model not in self.model_ids:
            raise ValueError("Choose a server-configured model")
        return replace(self, model=model, allowed_models=self.model_ids)

    def snapshot(self) -> ModelConfig:
        return ModelConfig(
            connection_id=self.connection_id,
            connection_revision=self.connection_revision,
            catalog_provider=self.catalog_provider,
            context_max_bytes=self.context_max_bytes,
            compact_tool_results=self.compact_tool_results if self.provider != "mock" else False,
            context_window_tokens=self.context_window_tokens,
            provider=self.provider,
            api_style=self.api_style,
            stream=self.stream if self.provider == "chat_completions" else False,
            context_policy=self.context_policy if self.provider == "chat_completions" else "reject",
            context_max_chars=self.context_max_chars,
            model=self.model if self.provider == "chat_completions" else "",
            route_fingerprint=(
                sha256(self.base_url.rstrip("/").encode()).hexdigest()
                if self.provider == "chat_completions"
                else ""
            ),
            timeout_seconds=self.timeout_seconds,
            max_rounds=self.max_rounds,
            max_completion_tokens=self.max_completion_tokens,
        )

    def for_snapshot(self, snapshot: ModelConfig) -> "ModelSettings":
        """Resolve credentials locally while preserving the submitted model and budgets."""
        if snapshot.provider == "mock":
            return ModelSettings(
                context_max_chars=snapshot.context_max_chars,
                timeout_seconds=snapshot.timeout_seconds,
                max_rounds=snapshot.max_rounds,
                max_completion_tokens=snapshot.max_completion_tokens,
            )
        if (self.connection_id, self.connection_revision) != (
            snapshot.connection_id,
            snapshot.connection_revision,
        ):
            raise ValueError("Worker credential revision does not match this run")
        if self.provider != snapshot.provider or (
            self.snapshot().route_fingerprint != snapshot.route_fingerprint
        ):
            raise ValueError("Worker does not serve the submitted model route")
        return replace(
            self,
            model=snapshot.model,
            api_style=snapshot.api_style,
            stream=snapshot.stream,
            context_policy=snapshot.context_policy,
            context_max_chars=snapshot.context_max_chars,
            context_max_bytes=snapshot.context_max_bytes,
            compact_tool_results=snapshot.compact_tool_results,
            context_window_tokens=snapshot.context_window_tokens,
            timeout_seconds=snapshot.timeout_seconds,
            max_rounds=snapshot.max_rounds,
            max_completion_tokens=snapshot.max_completion_tokens,
            require_credentials=True,
        )
