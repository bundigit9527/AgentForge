"""Explicit per-user grants and direct Worker-to-Supervisor Python execution."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunErrorCode
from agentforge_core.python_execution import ImageId, PythonResponse
from agentforge_runtime.errors import ExecutionStopped, RuntimeFailure
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PythonGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    users: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("users")
    @classmethod
    def explicit_users(cls, users):
        if any(not u.strip() or u == "*" or len(u) > 128 for u in users):
            raise ValueError("Specify explicit Python execution users")
        return users


class PythonPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grants: tuple[PythonGrant, ...] = Field(default=(), max_length=100)

    def permits(self, scope):
        return any(
            g.tenant_id == scope.tenant_id
            and g.space_id == scope.space_id
            and scope.user_id in g.users
            for g in self.grants
        )

    @classmethod
    def from_env(cls):
        path = os.environ.get("AGENTFORGE_PYTHON_POLICY_FILE", "")
        if not path:
            return cls()
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(128001)
            if len(data) > 128000:
                raise ValueError()
            return cls.model_validate_json(data)
        except (ValueError, OSError):
            raise ValueError("Invalid Python execution policy") from None


class PythonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image: ImageId
    endpoint: str
    token: str = Field(repr=False)

    @classmethod
    def from_env(cls):
        endpoint = os.environ.get("AGENTFORGE_PYTHON_ENDPOINT", "http://127.0.0.1:8766")
        token = os.environ.get("AGENTFORGE_MCP_COMPUTER_TOKEN", "")
        p = urlsplit(endpoint)
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username
            or p.password
            or p.path
            or p.query
            or p.fragment
            or not 32 <= len(token) <= 256
            or not token.isascii()
            or not token.isprintable()
            or " " in token
        ):
            raise ValueError("Configure Python Supervisor endpoint and service token")
        return cls(
            image=os.environ.get("AGENTFORGE_PYTHON_IMAGE", ""), endpoint=endpoint, token=token
        )


def prepare_python(scope, snapshot):
    image = None
    if snapshot.model.provider == "chat_completions" and PythonPolicy.from_env().permits(scope):
        image = PythonSettings.from_env().image
    legacy = any("python_execute@1" in s.manifest.required_tools for s in snapshot.skills)
    return ExecutionSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "python_image": image,
            "python_continuity": bool(image) and not legacy,
            "python_files": (),
        }
    )


class PythonService:
    def __init__(self, scope, image, checkpoint, *, transport=None):
        self.scope, self.image, self.checkpoint = scope, image, checkpoint
        self.transport = transport

    def authorized(self):
        if not self.checkpoint():
            raise ExecutionStopped()
        if (
            not PythonPolicy.from_env().permits(self.scope)
            or PythonSettings.from_env().image != self.image
        ):
            raise RuntimeFailure(RunErrorCode.TOOL_NOT_ALLOWED)

    def execute(self, arguments):
        self.authorized()
        config = PythonSettings.from_env()

        async def request():
            result = None
            async with httpx.AsyncClient(
                timeout=75, trust_env=False, transport=self.transport
            ) as client:
                async with anyio.create_task_group() as group:

                    async def send():
                        nonlocal result
                        async with client.stream(
                            "POST",
                            config.endpoint + "/python/execute",
                            headers={"Authorization": "Bearer " + config.token},
                            json={**arguments, "image": self.image},
                        ) as response:
                            response.raise_for_status()
                            data = bytearray()
                            async for part in response.aiter_bytes():
                                data.extend(part)
                                if len(data) > 14_100_000:
                                    raise ValueError("Python response too large")
                            result = PythonResponse.model_validate_json(data)
                        group.cancel_scope.cancel()

                    group.start_soon(send)
                    while True:
                        self.authorized()
                        await anyio.sleep(0.2)
            return result

        try:
            response = anyio.run(request)
        except BaseException as exc:
            self.authorized()
            if isinstance(exc, (Exception, BaseExceptionGroup)):
                raise RuntimeFailure(RunErrorCode.TOOL_EXECUTION_FAILED) from None
            raise
        self.authorized()
        return response
