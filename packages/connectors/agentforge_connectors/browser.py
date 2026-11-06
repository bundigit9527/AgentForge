"""Deployment grants and Worker-to-API browser transport. No Docker access or model URLs here."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from agentforge_core.browser import BrowserFailure, Origin, browser_result
from agentforge_core.models import RunErrorCode
from pydantic import BaseModel, ConfigDict, Field, model_validator


class BrowserGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str = Field(min_length=1, max_length=128)
    space_id: str = Field(min_length=1, max_length=128)
    users: tuple[str, ...] = Field(min_length=1, max_length=100)
    origins: tuple[Origin, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def explicit_users(self):
        if any(not u.strip() or len(u) > 128 or u == "*" for u in self.users):
            raise ValueError("Specify explicit browser users")
        return self


class BrowserPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    grants: tuple[BrowserGrant, ...] = Field(default=(), max_length=100)

    def origins(self, scope):
        return tuple(
            sorted(
                {
                    o
                    for g in self.grants
                    if (g.tenant_id, g.space_id) == (scope.tenant_id, scope.space_id)
                    and scope.user_id in g.users
                    for o in g.origins
                }
            )
        )

    @classmethod
    def from_env(cls):
        filename = os.environ.get("AGENTFORGE_BROWSER_POLICY_FILE", "")
        if not filename:
            return cls()
        try:
            with Path(filename).open("rb") as stream:
                data = stream.read(128001)
            if len(data) > 128000:
                raise ValueError()
            return cls.model_validate_json(data)
        except (ValueError, OSError):
            raise ValueError("Invalid browser policy file") from None


def service_configuration():
    endpoint = os.environ.get("AGENTFORGE_BROWSER_API_ORIGIN", "http://127.0.0.1:8000")
    p = urlsplit(endpoint)
    token = os.environ.get("AGENTFORGE_BROWSER_TOKEN", "")
    if (
        p.scheme not in ("http", "https")
        or not p.hostname
        or p.username
        or p.password
        or p.path
        or p.query
        or p.fragment
        or len(token) < 32
        or not token.isascii()
        or any(c.isspace() for c in token)
    ):
        raise ValueError("Configure a dedicated browser token and API origin")
    return endpoint, token


def browser_request(claim, arguments):
    try:
        origin, token = service_configuration()
        with httpx.Client(timeout=httpx.Timeout(260, connect=5), trust_env=False) as client:
            result = client.post(
                origin + "/api/internal/browser/execute",
                headers={"Authorization": "Bearer " + token},
                json={
                    "runId": claim.run.run_id,
                    "attemptId": claim.attempt_id,
                    "operation": arguments,
                },
            )
        if result.status_code != 200:
            code = result.json().get("error", "browser_unavailable")
            if code not in {e.value for e in RunErrorCode if e.value.startswith("browser_")}:
                code = "browser_unavailable"
            raise BrowserFailure(code)
        return browser_result(arguments["action"], result.json())
    except BrowserFailure:
        raise
    except Exception:
        raise BrowserFailure("browser_unavailable") from None
