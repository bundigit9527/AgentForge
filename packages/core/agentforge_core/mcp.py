"""Credential-free, pinned MCP tool contracts. Remote metadata grants no authority."""

import hashlib
import json
from typing import Annotated

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

ServerId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
RemoteName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def bounded_json(value, *, limit=20000, depth=12):
    if len(canonical(value)) > limit:
        raise ValueError("JSON exceeds the size limit")
    nodes = 0

    def walk(item, level):
        nonlocal nodes
        nodes += 1
        if level > depth or nodes > 2048:
            raise ValueError("JSON exceeds the complexity limit")
        if isinstance(item, dict):
            for child in item.values():
                walk(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, level + 1)

    walk(value, 0)
    return value


def validate_schema(value):
    bounded_json(value, limit=16000)
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("MCP tools require an object schema")

    def check(item):
        if isinstance(item, dict):
            # First release deliberately excludes references and regular expressions:
            # no remote resolution, recursive references or unbounded regex evaluation.
            if {
                "$ref",
                "$dynamicRef",
                "$recursiveRef",
                "pattern",
                "patternProperties",
            } & item.keys():
                raise ValueError("Unsupported MCP schema feature")
            if (
                "$schema" in item
                and item["$schema"] != "https://json-schema.org/draft/2020-12/schema"
            ):
                raise ValueError("Unsupported MCP schema dialect")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    try:
        Draft202012Validator.check_schema(value)
    except Exception:
        raise ValueError("Invalid MCP JSON Schema") from None
    return value


class McpSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: ServerId
    tool_name: RemoteName
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class McpToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: ServerId
    tool_name: RemoteName
    version: int = Field(ge=1, le=999999, strict=True)
    route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    description: str = Field(max_length=4000)
    input_schema: dict
    output_schema: dict | None = None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def name(self):
        return f"mcp_{self.server_id}_{hashlib.sha256(self.tool_name.encode()).hexdigest()[:12]}"

    @property
    def reference(self):
        return f"{self.name}@{self.version}"

    @model_validator(mode="after")
    def validate_definition(self):
        validate_schema(self.input_schema)
        if self.output_schema is not None:
            validate_schema(self.output_schema)
        if self.sha256 != self.digest(self.model_dump(exclude={"sha256"})):
            raise ValueError("MCP definition digest mismatch")
        return self

    @staticmethod
    def digest(fields):
        return hashlib.sha256(canonical(fields).encode()).hexdigest()

    def describe(self):
        return {
            **self.model_dump(exclude={"route_fingerprint"}),
            "reference": self.reference,
            "name": self.name,
            "effect": "read",
        }


class McpFailure(Exception):
    """Known public failure code, never an upstream exception or response body."""

    def __init__(self, code="mcp_unavailable"):
        if code not in {
            "mcp_unavailable",
            "mcp_invalid_response",
            "mcp_timeout",
            "mcp_tool_failed",
            "mcp_definition_changed",
            "mcp_not_allowed",
        }:
            raise ValueError("Unknown MCP failure code")
        self.code = code
        super().__init__(code)


class McpCancelled(Exception):
    pass
