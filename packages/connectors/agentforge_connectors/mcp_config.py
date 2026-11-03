"""Deployment-owned endpoints and grants; never accept a URL or token from the model."""

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from agentforge_core.mcp import RemoteName, ServerId, canonical
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class McpServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: ServerId
    tenant_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    users: tuple[str, ...] = Field(min_length=1, max_length=100)
    url: str = Field(repr=False, max_length=2048)
    token_env: str = Field(default="", repr=False)
    read_tools: dict[RemoteName, int] = Field(min_length=1, max_length=16)
    allow_insecure_http: bool = Field(default=False, strict=True)
    timeout_seconds: float = Field(default=15, gt=0, le=60, allow_inf_nan=False)

    @field_validator("users")
    @classmethod
    def valid_users(cls, value):
        if len(set(value)) != len(value) or any(
            not u.strip() or len(u) > 128 or u == "*" for u in value
        ):
            raise ValueError("Specify explicit MCP user IDs")
        return value

    @field_validator("read_tools", mode="before")
    @classmethod
    def valid_versions(cls, value):
        if not isinstance(value, dict) or any(
            type(v) is not int or not 1 <= v <= 999999 for v in value.values()
        ):
            raise ValueError("Read tools require explicit positive versions")
        return value

    @model_validator(mode="after")
    def valid_endpoint(self):
        import re

        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(c.isspace() for c in self.url)
            or (url.scheme == "http" and not self.allow_insecure_http)
        ):
            raise ValueError("Use an approved HTTPS MCP endpoint")
        _ = url.port
        if self.token_env and not re.fullmatch(r"AGENTFORGE_MCP_[A-Z0-9_]+_TOKEN", self.token_env):
            raise ValueError("Use a dedicated MCP token environment variable")
        return self

    def permits(self, scope):
        return (self.tenant_id, self.space_id) == (
            scope.tenant_id,
            scope.space_id,
        ) and scope.user_id in self.users

    @property
    def fingerprint(self):
        return hashlib.sha256(
            canonical([self.url, self.token_env, self.tenant_id, self.space_id]).encode()
        ).hexdigest()

    def token(self):
        value = os.environ.get(self.token_env, "") if self.token_env else ""
        if self.token_env and (not value or any(c.isspace() for c in value)):
            from agentforge_core.mcp import McpFailure

            raise McpFailure("mcp_unavailable")
        return value


class McpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    servers: tuple[McpServer, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique_servers(self):
        if len({s.server_id for s in self.servers}) != len(self.servers):
            raise ValueError("Duplicate MCP server IDs")
        return self

    @classmethod
    def from_env(cls):
        filename = os.environ.get("AGENTFORGE_MCP_SERVERS_FILE", "")
        if not filename:
            return cls()
        try:
            with Path(filename).open("rb") as stream:
                content = stream.read(128001)
            if len(content) > 128000:
                raise ValueError()
            return cls.model_validate_json(content)
        except (OSError, ValueError):
            raise ValueError("Invalid MCP configuration; check the local servers file") from None
