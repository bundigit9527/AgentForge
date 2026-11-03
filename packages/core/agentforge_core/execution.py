"""Versioned, credential-free inputs persisted before a run enters the queue."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge_core.mcp import McpToolSnapshot
from agentforge_core.skills import SkillReference, SkillSnapshot
from agentforge_core.tools import ToolReferences


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["mock", "chat_completions"] = "mock"
    model: str = Field(default="", max_length=128)
    api_style: Literal["openai", "deepseek"] = "openai"
    stream: bool = Field(default=False, strict=True)
    route_fingerprint: str = ""
    # Missing fields on historical snapshots retain the former reject-on-overflow policy.
    context_policy: Literal["reject", "trim_history"] = "reject"
    context_max_chars: int = Field(default=100000, ge=4000, le=200000, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_rounds: int = Field(default=8, ge=1, le=32, strict=True)
    max_completion_tokens: int = Field(default=2048, ge=1, le=8192, strict=True)

    @model_validator(mode="after")
    def validate_route(self):
        if self.provider == "chat_completions":
            if not self.model.strip() or len(self.route_fingerprint) != 64:
                raise ValueError("A model and route fingerprint are required")
        elif self.model or self.route_fingerprint or self.api_style != "openai":
            raise ValueError("Mock runs cannot select a remote route")
        return self


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    harness_version: Literal[1] = 1
    authorization_mode: Literal["development", "database"] = "development"
    model: ModelConfig = Field(default_factory=ModelConfig)
    instructions: str = Field(default="", max_length=10000)
    tools: ToolReferences = ("example_report@1",)
    approval_tools: ToolReferences = ()
    mcp_tools: tuple[McpToolSnapshot, ...] = Field(default=(), max_length=8)
    skills: tuple[SkillSnapshot, ...] = Field(default=(), max_length=8)
    # Optional per-run composer selection; older snapshots default to no explicit choice.
    requested_skill: SkillReference | None = None

    @model_validator(mode="after")
    def validate_skills(self):
        if self.mcp_tools and self.model.provider != "chat_completions":
            raise ValueError("MCP tools require a real-model execution profile")
        refs = [*self.tools, *(t.reference for t in self.mcp_tools)]
        if not set(self.approval_tools) <= set(refs):
            raise ValueError("Approval tools must be enabled tools")
        if self.approval_tools and self.model.provider != "chat_completions":
            raise ValueError("Approval requires Chat Completions")
        if len(refs) > 16 or len(set(ref.split("@")[0] for ref in refs)) != len(refs):
            raise ValueError("Select at most 16 distinct tools")
        if self.requested_skill is not None and self.requested_skill not in {
            skill.reference for skill in self.skills
        }:
            raise ValueError("Requested skill must belong to this snapshot")
        identities = [(s.source_id, s.manifest.skill_id) for s in self.skills]
        if len(set(identities)) != len(identities):
            raise ValueError("Select only one version of each skill")
        if sum(len(s.manifest.content) for s in self.skills) > 32000:
            raise ValueError("Selected skill recipes exceed the context budget")
        if any(not set(s.manifest.required_tools) <= set(refs) for s in self.skills):
            raise ValueError("A selected skill requires tools not enabled on this Agent")
        return self


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str
