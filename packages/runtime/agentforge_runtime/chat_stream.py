"""Bounded SSE decoding and complete tool-call assembly before tool execution."""

import codecs
import time
from typing import Literal

from agentforge_core.models import RunErrorCode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentforge_runtime.chat_model import MAX_RESPONSE_BYTES, Completion, Usage
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure


class FunctionDelta(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str | None = Field(default=None, max_length=128)
    arguments: str | None = Field(default=None, max_length=20000)


class CallDelta(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int = Field(ge=0, le=7)
    id: str | None = Field(default=None, max_length=128)
    type: Literal["function"] | None = None
    function: FunctionDelta | None = None


class Delta(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["assistant"] | None = None
    content: str | None = Field(default=None, max_length=20000)
    tool_calls: list[CallDelta] | None = Field(default=None, max_length=8)
    refusal: str | None = Field(default=None, max_length=20000)


class StreamChoice(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int = Field(ge=0, le=0)
    delta: Delta
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] | None = None


class StreamChunk(BaseModel):
    model_config = ConfigDict(strict=True)
    choices: list[StreamChoice] = Field(max_length=1)
    usage: Usage | None = None


def invalid():
    return RuntimeFailure(RunErrorCode.MODEL_INVALID_RESPONSE)


def sse_data(response, settings, started, checkpoint):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending, fields = "", []
    size = 0
    for chunk in response.iter_bytes():
        if not checkpoint():
            raise ExecutionStopped()
        if time.monotonic() - started > settings.timeout_seconds:
            raise RuntimeFailure(RunErrorCode.MODEL_TIMEOUT)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if fields:
                    yield "\n".join(fields)
                    fields = []
            elif line.startswith("data:"):
                fields.append(line[5:].removeprefix(" "))
            elif line.startswith("event:") and line[6:].strip() == "error":
                raise invalid()
    pending += decoder.decode(b"", final=True)
    # SSE frames must end in a blank line, including the DONE sentinel.
    if pending or fields:
        raise invalid()


def read_stream(response, settings, started, on_text, checkpoint):
    if response.headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
        raise invalid()
    text, calls, usage, finish, refusal = "", {}, None, None, None
    done = False
    try:
        for data in sse_data(response, settings, started, checkpoint):
            if not checkpoint():
                raise ExecutionStopped()
            if data == "[DONE]":
                if finish is None:
                    raise invalid()
                done = True
                break
            part = StreamChunk.model_validate_json(data)
            if part.usage is not None:
                if usage is not None:
                    raise invalid()
                usage = part.usage
            if not part.choices:
                if finish is None or part.usage is None:
                    raise invalid()
                continue
            if finish is not None:
                raise invalid()
            choice = part.choices[0]
            delta = choice.delta
            if delta.refusal:
                refusal = delta.refusal
            if delta.content:
                text += delta.content
                if len(text) > 20000:
                    raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
                if on_text:
                    on_text(delta.content)
            indices = set()
            for call in delta.tool_calls or []:
                if call.index in indices:
                    raise invalid()
                indices.add(call.index)
                target = calls.setdefault(
                    call.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call.id:
                    target["id"] += call.id
                if call.function:
                    target["function"]["name"] += call.function.name or ""
                    target["function"]["arguments"] += call.function.arguments or ""
                if (
                    len(target["id"]) > 128
                    or len(target["function"]["name"]) > 128
                    or len(target["function"]["arguments"]) > 20000
                ):
                    raise RuntimeFailure(RunErrorCode.MODEL_LIMIT_EXCEEDED)
            finish = choice.finish_reason
        if not done or sorted(calls) != list(range(len(calls))):
            raise invalid()
        return Completion.model_validate(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": text or None,
                            "tool_calls": [calls[i] for i in sorted(calls)] or None,
                            "refusal": refusal,
                        },
                        "finish_reason": finish,
                    }
                ],
                "usage": usage,
            }
        )
    except (ValidationError, UnicodeError, ValueError):
        raise invalid() from None
