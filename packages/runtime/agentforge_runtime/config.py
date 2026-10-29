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
    timeout_seconds: float = 30
    max_rounds: int = 8
    max_completion_tokens: int = 2048
    require_credentials: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.provider not in {"mock", "chat_completions"}:
            raise ValueError("AGENTFORGE_MODEL_PROVIDER must be mock or chat_completions")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("AGENTFORGE_MODEL_TIMEOUT_SECONDS must be in (0, 120]")
        if type(self.max_rounds) is not int or not 1 <= self.max_rounds <= 32:
            raise ValueError("AGENTFORGE_MODEL_MAX_ROUNDS must be between 1 and 32")
        if (
            type(self.max_completion_tokens) is not int
            or not 1 <= self.max_completion_tokens <= 8192
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
            if self.require_credentials and (
                not self.api_key.strip() or any(char.isspace() for char in self.api_key)
            ):
                raise ValueError("AGENTFORGE_MODEL_API_KEY must be a token without whitespace")
            if not self.model.strip() or len(self.model) > 128:
                raise ValueError("AGENTFORGE_MODEL_NAME must contain 1–128 characters")

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> "ModelSettings":
        try:
            timeout = float(os.environ.get("AGENTFORGE_MODEL_TIMEOUT_SECONDS", "30"))
            rounds = int(os.environ.get("AGENTFORGE_MODEL_MAX_ROUNDS", "8"))
            tokens = int(os.environ.get("AGENTFORGE_MODEL_MAX_COMPLETION_TOKENS", "2048"))
        except ValueError:
            raise ValueError(
                "Invalid numeric model configuration; check timeout, rounds and token limit"
            ) from None
        return cls(
            provider=os.environ.get("AGENTFORGE_MODEL_PROVIDER", "mock"),
            base_url=os.environ.get("AGENTFORGE_MODEL_BASE_URL", ""),
            api_key=os.environ.get("AGENTFORGE_MODEL_API_KEY", "") if require_credentials else "",
            model=os.environ.get("AGENTFORGE_MODEL_NAME", ""),
            timeout_seconds=timeout,
            max_rounds=rounds,
            max_completion_tokens=tokens,
            require_credentials=require_credentials,
        )

    def snapshot(self) -> ModelConfig:
        return ModelConfig(
            provider=self.provider,
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
                timeout_seconds=snapshot.timeout_seconds,
                max_rounds=snapshot.max_rounds,
                max_completion_tokens=snapshot.max_completion_tokens,
            )
        if self.provider != snapshot.provider or (
            self.snapshot().route_fingerprint != snapshot.route_fingerprint
        ):
            raise ValueError("Worker does not serve the submitted model route")
        return replace(
            self,
            model=snapshot.model,
            timeout_seconds=snapshot.timeout_seconds,
            max_rounds=snapshot.max_rounds,
            max_completion_tokens=snapshot.max_completion_tokens,
            require_credentials=True,
        )
