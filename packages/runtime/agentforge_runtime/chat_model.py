import time
from collections.abc import Callable
from typing import Literal, Protocol

import httpx
from agentforge_core.models import RunErrorCode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentforge_runtime.config import ModelSettings
from agentforge_runtime.errors import ExecutionStopped
from agentforge_runtime.errors import RuntimeFailure as RuntimeFailure

MAX_RESPONSE_BYTES = 1024 * 1024


class Function(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(min_length=1, max_length=128)
    arguments: str = Field(max_length=20000)


class FunctionCall(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"]
    function: Function


class AssistantMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["assistant"]
    content: str | None = Field(default=None, max_length=20000)
    tool_calls: list[FunctionCall] | None = Field(default=None, max_length=8)
    refusal: str | None = None


class Usage(BaseModel):
    model_config = ConfigDict(strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class Choice(BaseModel):
    message: AssistantMessage
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]


class Completion(BaseModel):
    choices: list[Choice] = Field(min_length=1, max_length=1)
    usage: Usage | None = None


class ChatModel(Protocol):
    name: str

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        on_text: Callable[[str], None] | None = None,
        checkpoint: Callable[[], bool] = lambda: True,
        tool_choice: dict | None = None,
    ) -> Completion: ...


class ChatCompletionsModel:
    def __init__(self, settings: ModelSettings, *, transport: httpx.BaseTransport | None = None):
        if settings.provider != "chat_completions":
            raise ValueError("Chat model requires chat_completions configuration")
        self.settings = settings
        self.name = settings.model
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        on_text: Callable[[str], None] | None = None,
        checkpoint: Callable[[], bool] = lambda: True,
        tool_choice: dict | None = None,
    ) -> Completion:
        if not checkpoint():
            raise ExecutionStopped()
        started = time.monotonic()
        try:
            with self._client.stream(
                "POST",
                self.settings.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.api_key}"}
                if self.settings.api_key
                else {},
                json={
                    "model": self.name,
                    "messages": messages,
                    **({"tools": tools, "tool_choice": tool_choice or "auto"} if tools else {}),
                    "stream": self.settings.stream,
                    **({"stream_options": {"include_usage": True}} if self.settings.stream else {}),
                    "n": 1,
                    **(
                        {
                            "max_tokens": self.settings.max_completion_tokens,
                            "thinking": {"type": "disabled"},
                        }
                        if self.settings.api_style == "deepseek"
                        else {
                            (
                                "max_tokens"
                                if self.settings.api_style == "compatible"
                                else "max_completion_tokens"
                            ): self.settings.max_completion_tokens
                        }
                    ),
                },
            ) as response:
                if response.status_code in {401, 403}:
                    raise RuntimeFailure(RunErrorCode.MODEL_AUTH_FAILED)
                if response.status_code == 429:
                    raise RuntimeFailure(RunErrorCode.MODEL_RATE_LIMITED)
                if response.status_code != 200:
                    raise RuntimeFailure(RunErrorCode.MODEL_UNAVAILABLE)
                if self.settings.stream:
                    # Imported after protocol models are initialized to share validation.
                    from agentforge_runtime.chat_stream import read_stream

                    result = read_stream(response, self.settings, started, on_text, checkpoint)
                else:
                    result = self._read_json(response, started, checkpoint)
        except httpx.TimeoutException:
            raise RuntimeFailure(RunErrorCode.MODEL_TIMEOUT) from None
        except httpx.HTTPError:
            raise RuntimeFailure(RunErrorCode.MODEL_UNAVAILABLE) from None
        except ValidationError:
            raise RuntimeFailure(RunErrorCode.MODEL_INVALID_RESPONSE) from None
        return validate_completion(result)

    def _read_json(self, response, started, checkpoint):
        body = bytearray()
        for chunk in response.iter_bytes():
            if not checkpoint():
                raise ExecutionStopped()
            if time.monotonic() - started > self.settings.timeout_seconds:
                raise RuntimeFailure(RunErrorCode.MODEL_TIMEOUT)
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
            body.extend(chunk)
        return Completion.model_validate_json(bytes(body))


def validate_completion(result: Completion) -> Completion:
    choice = result.choices[0]
    if choice.finish_reason == "length":
        raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
    if choice.finish_reason == "content_filter" or choice.message.refusal:
        raise RuntimeFailure(RunErrorCode.MODEL_REFUSED)
    calls = choice.message.tool_calls
    if (choice.finish_reason == "tool_calls") != bool(calls):
        raise RuntimeFailure(RunErrorCode.MODEL_INVALID_RESPONSE)
    if not calls and not (choice.message.content or "").strip():
        raise RuntimeFailure(RunErrorCode.MODEL_INVALID_RESPONSE)
    return result
