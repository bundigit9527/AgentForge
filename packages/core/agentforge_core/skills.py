"""Skill recipes are versioned data; they never grant executable capabilities."""

import hashlib
import json
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge_core.tools import ToolReferences

SkillId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
SkillVersion = Annotated[int, Field(ge=1, le=999999, strict=True)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
SkillReference = Annotated[
    str,
    Field(
        pattern=r"^(builtin:[a-z][a-z0-9_-]{0,63}@[1-9][0-9]{0,5}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$"
    ),
]


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: SkillId
    version: SkillVersion
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    required_tools: ToolReferences = ()

    @field_validator("name", "description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Skill text must not be blank")
        return value


class SkillManifest(SkillMetadata):
    content: str = Field(min_length=1, max_length=12000)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value):
        return cls.nonblank(value)

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SkillSummary(SkillMetadata):
    sha256: Digest


class HubCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SkillSummary] = Field(max_length=50)
    next_offset: int | None = Field(default=None, ge=0)


class SkillSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: SkillReference
    source: Literal["builtin", "hub"]
    source_id: str = Field(pattern=r"^(builtin|hub:[a-f0-9]{64})$")
    manifest: SkillManifest
    sha256: Digest

    @model_validator(mode="after")
    def consistent(self):
        builtin = self.source == "builtin"
        expected = f"builtin:{self.manifest.skill_id}@{self.manifest.version}"
        if builtin != (self.source_id == "builtin") or builtin != self.reference.startswith(
            "builtin:"
        ):
            raise ValueError("Invalid skill source")
        if builtin and self.reference != expected:
            raise ValueError("Invalid builtin reference")
        if self.sha256 != self.manifest.digest():
            raise ValueError("Skill content digest does not match")
        return self


class HubUnavailable(Exception):
    """Public errors must not contain transport URLs, credentials or response bodies."""


class HubInvalidResponse(Exception):
    pass


class SkillHub(Protocol):
    @property
    def source_id(self) -> str: ...
    def catalog(self, offset: int, limit: int, query: str) -> HubCatalog: ...
    def manifest(self, skill_id: str, version: int) -> SkillManifest: ...
