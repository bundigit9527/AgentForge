"""Expose approved remote reads through the same tool journal and lease boundary."""

from copy import deepcopy
from typing import Any

from agentforge_core.mcp import McpCancelled, McpFailure, bounded_json
from agentforge_core.models import RunErrorCode
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from agentforge_runtime.tools import ToolDefinition


class McpResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(max_length=20000)
    structured: dict | None = None


def definition(service, scope, snapshot, checkpoint, bot_id=None):
    binding = service.check(scope, snapshot).bot_id
    if binding is not None and str(binding) != bot_id:
        raise McpFailure("mcp_not_allowed")
    schema = deepcopy(snapshot.input_schema)

    class Arguments(RootModel[dict[str, Any]]):
        @model_validator(mode="after")
        def validate_arguments(self):
            try:
                bounded_json(self.root)
                Draft202012Validator(schema).validate(self.root)
            except Exception:
                raise ValueError("Invalid MCP tool arguments") from None
            return self

        @classmethod
        def model_json_schema(cls, **kwargs):
            return deepcopy(schema)

    def execute(arguments):
        try:
            return service.call(scope, snapshot, arguments.root, checkpoint)
        except McpCancelled:
            raise ExecutionStopped() from None
        except McpFailure as exc:
            raise RuntimeFailure(RunErrorCode(exc.code)) from None

    return ToolDefinition(
        snapshot.name,
        snapshot.version,
        f"MCP {snapshot.server_id}/{snapshot.tool_name}: {snapshot.description}",
        Arguments,
        McpResult,
        execute,
        effect="read",
    )


def with_mcp_tools(registry, service, scope, snapshots, checkpoint, *, bot_id=None):
    try:
        return registry.extended(
            definition(service, scope, item, checkpoint, bot_id) for item in snapshots
        )
    except McpFailure as exc:
        raise RuntimeFailure(RunErrorCode(exc.code)) from None
