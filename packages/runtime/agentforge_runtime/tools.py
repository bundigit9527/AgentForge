"""Explicit tool registration and a common execution boundary for every Harness."""

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from agentforge_core.models import RunErrorCode
from agentforge_core.tools import ToolInvocation, ToolTicket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure


class TextArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=10000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value


class ReportResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=20000)


class TextStatistics(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    characters: int = Field(ge=0)
    non_whitespace_characters: int = Field(ge=0)
    lines: int = Field(ge=0)
    whitespace_tokens: int = Field(ge=0)


def report(arguments: TextArguments) -> dict:
    return {"text": "# 报告草稿\n\n" + arguments.text}


def statistics(arguments: TextArguments) -> dict:
    return {
        "characters": len(arguments.text),
        "non_whitespace_characters": sum(not char.isspace() for char in arguments.text),
        "lines": len(arguments.text.splitlines()),
        "whitespace_tokens": len(arguments.text.split()),
    }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: int
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[BaseModel], dict]
    effect: Literal["pure", "read", "write"] = "pure"
    render: Callable[[BaseModel], str] = lambda output: output.model_dump_json()

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    def function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }

    def describe(self) -> dict:
        return {
            "reference": self.reference,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "effect": self.effect,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition]):
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", definition.name) or (
                type(definition.version) is not int or not 1 <= definition.version <= 999999
            ):
                raise ValueError("Invalid tool identity")
            if definition.reference in self._definitions:
                raise ValueError("Duplicate tool version")
            self._definitions[definition.reference] = definition

    def extended(self, definitions: Iterable[ToolDefinition]) -> "ToolRegistry":
        return ToolRegistry([*self._definitions.values(), *definitions])

    def validate_allowlist(self, references: tuple[str, ...]) -> None:
        if len(set(references)) != len(references) or any(
            ref not in self._definitions for ref in references
        ):
            raise ValueError("Tool allowlist contains duplicate or unknown versions")

    def select(
        self, references: tuple[str, ...], allowed: tuple[str, ...]
    ) -> dict[str, ToolDefinition]:
        selected = {}
        for ref in references:
            definition = self._definitions.get(ref)
            # External reads/writes require future approvals, credentials and effect recovery.
            if definition is None or ref not in allowed or definition.effect != "pure":
                raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
            if definition.name in selected:
                raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
            selected[definition.name] = definition
        return selected

    def catalog(self, allowed: tuple[str, ...]) -> list[dict]:
        return [
            definition.describe()
            for ref, definition in self._definitions.items()
            if ref in allowed and definition.effect == "pure"
        ]


DEFAULT_TOOLS = ("example_report@1", "text_stats@1")
BUILTIN_TOOLS = ToolRegistry(
    [
        ToolDefinition(
            "example_report",
            1,
            "Format supplied text as a report draft. Does not browse or execute commands.",
            TextArguments,
            ReportResult,
            report,
            render=lambda output: output.text,
        ),
        ToolDefinition(
            "text_stats",
            1,
            "Count Unicode code points, lines and whitespace-separated tokens in text. "
            "Does not browse, tokenize Chinese words or count model tokens.",
            TextArguments,
            TextStatistics,
            statistics,
        ),
    ]
)


def allowed_tools_from_env() -> tuple[str, ...]:
    raw = os.environ.get("AGENTFORGE_ALLOWED_TOOLS", ",".join(DEFAULT_TOOLS))
    references = tuple(part.strip() for part in raw.split(",")) if raw.strip() else ()
    BUILTIN_TOOLS.validate_allowlist(references)
    return references


@dataclass(frozen=True)
class PreparedToolCall:
    definition: ToolDefinition
    arguments: BaseModel
    invocation: ToolInvocation


class ToolJournal(Protocol):
    def start(self, invocation: ToolInvocation) -> ToolTicket | None: ...
    def complete(self, ticket: ToolTicket, output: dict, content: str) -> bool: ...
    def fail(self, ticket: ToolTicket, code: RunErrorCode) -> bool: ...


class ToolExecutor:
    def __init__(
        self,
        references: tuple[str, ...] = ("example_report@1",),
        *,
        registry: ToolRegistry = BUILTIN_TOOLS,
        allowed: tuple[str, ...] = DEFAULT_TOOLS,
        journal: ToolJournal | None = None,
        checkpoint: Callable[[], bool] = lambda: True,
    ):
        self.definitions = registry.select(references, allowed)
        self.journal = journal
        self.checkpoint = checkpoint

    def schemas(self) -> list[dict]:
        return [definition.function_schema() for definition in self.definitions.values()]

    def prepare(self, call_id: str, name: str, arguments_json: str) -> PreparedToolCall:
        definition = self.definitions.get(name)
        if definition is None:
            raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
        if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 128:
            raise RuntimeFailure(RunErrorCode.TOOL_CALL_CONFLICT)
        try:
            arguments = definition.input_model.model_validate_json(arguments_json)
            canonical = json.dumps(
                arguments.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(canonical) > 20000:
                raise ValueError("Tool arguments are too large")
        except (ValidationError, ValueError):
            raise RuntimeFailure(RunErrorCode.TOOL_INVALID_ARGUMENTS) from None
        return PreparedToolCall(
            definition,
            arguments,
            ToolInvocation(
                call_id,
                definition.name,
                definition.version,
                canonical,
            ),
        )

    def execute(self, prepared: PreparedToolCall) -> str:
        if not self.checkpoint():
            raise ExecutionStopped()
        if self.definitions.get(prepared.definition.name) is not prepared.definition:
            raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)
        ticket = self.journal.start(prepared.invocation) if self.journal else None
        if self.journal and ticket is None:
            raise ExecutionStopped()
        if ticket is not None and ticket.cached:
            if not self.checkpoint():
                raise ExecutionStopped()
            if ticket.content is None:
                raise RuntimeFailure(RunErrorCode.TOOL_CALL_CONFLICT)
            return ticket.content
        if not self.checkpoint():
            raise ExecutionStopped()
        try:
            try:
                raw = prepared.definition.handler(prepared.arguments)
            except Exception:
                raise RuntimeFailure(RunErrorCode.TOOL_EXECUTION_FAILED) from None
            try:
                output = prepared.definition.output_model.model_validate(raw)
                content = prepared.definition.render(output)
                if not isinstance(content, str) or len(content) > 20000:
                    raise ValueError("Invalid tool result size")
                serialized = output.model_dump(mode="json")
                if len(json.dumps(serialized, ensure_ascii=False)) > 20000:
                    raise ValueError("Invalid tool result size")
            except Exception:
                raise RuntimeFailure(RunErrorCode.TOOL_INVALID_RESULT) from None
        except RuntimeFailure as error:
            if not self.checkpoint() or (
                self.journal and not self.journal.fail(ticket, error.code)
            ):
                raise ExecutionStopped() from None
            raise
        if not self.checkpoint() or (
            self.journal and not self.journal.complete(ticket, serialized, content)
        ):
            raise ExecutionStopped()
        return content
