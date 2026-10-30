"""Versioned, credential-free inputs persisted before a run enters the queue."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge_core.skills import SkillSnapshot
from agentforge_core.tools import ToolReferences


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["mock", "chat_completions"] = "mock"
    model: str = Field(default="", max_length=128)
    route_fingerprint: str = ""
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_rounds: int = Field(default=8, ge=1, le=32, strict=True)
    max_completion_tokens: int = Field(default=2048, ge=1, le=8192, strict=True)

    @model_validator(mode="after")
    def validate_route(self):
        if self.provider == "chat_completions":
            if not self.model.strip() or len(self.route_fingerprint) != 64:
                raise ValueError("A model and route fingerprint are required")
        elif self.model or self.route_fingerprint:
            raise ValueError("Mock runs cannot select a remote route")
        return self


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    harness_version: Literal[1] = 1
    model: ModelConfig = Field(default_factory=ModelConfig)
    instructions: str = Field(default="", max_length=10000)
    tools: ToolReferences = ("example_report@1",)
    skills: tuple[SkillSnapshot, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_skills(self):
        identities = [(s.source_id, s.manifest.skill_id) for s in self.skills]
        if len(set(identities)) != len(identities):
            raise ValueError("Select only one version of each skill")
        if sum(len(s.manifest.content) for s in self.skills) > 32000:
            raise ValueError("Selected skill recipes exceed the context budget")
        if any(not set(s.manifest.required_tools) <= set(self.tools) for s in self.skills):
            raise ValueError("A selected skill requires tools not enabled on this Agent")
        return self


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str
