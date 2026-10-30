"""AgentForge's provisional HTTP JSON contract, replaceable for an existing Hub."""

import math
import os
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
from agentforge_core.models import RunNotFound
from agentforge_core.skills import HubCatalog, HubInvalidResponse, HubUnavailable, SkillManifest
from pydantic import ValidationError


@dataclass(frozen=True)
class SkillHubSettings:
    base_url: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    timeout_seconds: float = 10

    def __post_init__(self):
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60:
            raise ValueError("Skill Hub timeout must be in (0, 60]")
        if any(char.isspace() for char in self.token):
            raise ValueError("Skill Hub token must not contain whitespace")
        if not self.base_url:
            return
        try:
            url = urlsplit(self.base_url)
            valid = (
                url.scheme in {"https", "http"}
                and url.hostname
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
            raise ValueError("Skill Hub requires an HTTP(S) base URL without credentials or query")

    @classmethod
    def from_env(cls):
        try:
            timeout = float(os.environ.get("AGENTFORGE_SKILL_HUB_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise ValueError("Invalid Skill Hub timeout") from None
        return cls(
            os.environ.get("AGENTFORGE_SKILL_HUB_URL", ""),
            os.environ.get("AGENTFORGE_SKILL_HUB_TOKEN", ""),
            timeout,
        )


class HttpSkillHub:
    def __init__(self, settings: SkillHubSettings, *, transport=None):
        self.settings = settings
        self.client = httpx.Client(
            transport=transport,
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {settings.token}"} if settings.token else {},
        )

    @property
    def source_id(self):
        if not self.settings.base_url:
            raise HubUnavailable()
        return "hub:" + sha256(self.settings.base_url.rstrip("/").encode()).hexdigest()

    def _get(self, path, model, params=None):
        if not self.settings.base_url:
            raise HubUnavailable()
        try:
            with self.client.stream(
                "GET", self.settings.base_url.rstrip("/") + path, params=params
            ) as response:
                if response.status_code == 404:
                    raise RunNotFound()
                if not 200 <= response.status_code < 300:
                    raise HubUnavailable()
                payload = bytearray()
                for chunk in response.iter_bytes(chunk_size=16384):
                    payload.extend(chunk)
                    if len(payload) > 512000:
                        raise HubInvalidResponse()
            return model.model_validate_json(bytes(payload))
        except (httpx.HTTPError, httpx.InvalidURL):
            raise HubUnavailable() from None
        except (ValidationError, ValueError):
            raise HubInvalidResponse() from None

    def catalog(self, offset: int, limit: int, query: str) -> HubCatalog:
        page = self._get("/skills", HubCatalog, {"offset": offset, "limit": limit, "q": query})
        identities = {(item.skill_id, item.version) for item in page.items}
        if (
            len(page.items) > limit
            or len(identities) != len(page.items)
            or (page.next_offset is not None and page.next_offset != offset + len(page.items))
            or (not page.items and page.next_offset is not None)
        ):
            raise HubInvalidResponse()
        return page

    def manifest(self, skill_id: str, version: int) -> SkillManifest:
        # Validate before constructing a URL, including calls outside the HTTP API.
        from agentforge_core.skills import SkillId, SkillVersion
        from pydantic import TypeAdapter

        TypeAdapter(SkillId).validate_python(skill_id)
        TypeAdapter(SkillVersion).validate_python(version)
        manifest = self._get(f"/skills/{skill_id}/versions/{version}", SkillManifest)
        if manifest.skill_id != skill_id or manifest.version != version:
            raise HubInvalidResponse()
        return manifest

    def close(self):
        self.client.close()
