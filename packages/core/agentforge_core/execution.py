"""Versioned, credential-free inputs persisted before a run enters the queue."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge_core.browser import Origin, WebUrl, browser_tools, permitted_url
from agentforge_core.mcp import McpToolSnapshot
from agentforge_core.reports import exports_csv
from agentforge_core.skills import SkillReference, SkillSnapshot
from agentforge_core.tools import ToolReferences


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["mock", "chat_completions"] = "mock"
    model: str = Field(default="", max_length=128)
    api_style: Literal["openai", "deepseek", "compatible"] = "openai"
    stream: bool = Field(default=False, strict=True)
    route_fingerprint: str = ""
    connection_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    connection_revision: int | None = Field(default=None, ge=1, strict=True)
    catalog_provider: Literal["deepseek", "openai-compatible"] | None = None
    context_max_bytes: int | None = Field(default=None, ge=1024, le=2147483647, strict=True)
    # Missing fields on historical snapshots retain the former reject-on-overflow policy.
    context_policy: Literal["reject", "trim_history"] = "reject"
    context_max_chars: int = Field(default=100000, ge=4000, le=200000, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_rounds: int = Field(default=8, ge=1, le=32, strict=True)
    max_completion_tokens: int = Field(default=2048, ge=1, le=131072, strict=True)

    @model_validator(mode="after")
    def validate_route(self):
        personal = (self.connection_id, self.connection_revision, self.catalog_provider)
        if any(x is not None for x in personal) and (
            any(x is None for x in personal) or self.provider != "chat_completions"
        ):
            raise ValueError("Personal routes require a complete credential reference")
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
    # API-frozen discovery catalog; historical/REST snapshots remain opt-in.
    catalog_skills: tuple[SkillSnapshot, ...] = Field(default=(), max_length=32)
    catalog_omitted: int = Field(default=0, ge=0, strict=True)
    # Optional per-run composer selection; older snapshots default to no explicit choice.
    requested_skill: SkillReference | None = None
    browser_origins: tuple[Origin, ...] = Field(default=(), max_length=32)
    # Historical runs retain their original tool set.
    browser_reading: bool = Field(default=False, strict=True)
    web_origins: tuple[Origin, ...] = Field(default=(), max_length=32)
    web_search_endpoint: WebUrl | None = None
    python_image: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    model_backups: tuple[ModelConfig, ...] = Field(default=(), max_length=10)

    @property
    def available_skills(self) -> tuple[SkillSnapshot, ...]:
        return self.skills + self.catalog_skills

    @property
    def tool_references(self) -> tuple[str, ...]:
        return (
            *self.tools,
            *(("python_execute@1",) if self.python_image else ()),
            *(("web_fetch@1",) if self.web_origins else ()),
            *(("web_search@1",) if self.web_search_endpoint else ()),
            *(t.reference for t in self.mcp_tools),
            *(browser_tools(self.browser_reading) if self.browser_origins else ()),
        )

    @model_validator(mode="after")
    def validate_skills(self):
        if self.python_image and self.model.provider != "chat_completions":
            raise ValueError("Python execution requires Chat Completions")
        if self.web_origins and self.model.provider != "chat_completions":
            raise ValueError("Web tools require Chat Completions")
        if self.web_search_endpoint and not permitted_url(
            self.web_search_endpoint, self.web_origins
        ):
            raise ValueError("Search endpoint requires a matching web grant")
        if self.model_backups and (
            self.model.provider != "chat_completions"
            or any(m.provider != "chat_completions" for m in self.model_backups)
        ):
            raise ValueError("Backup models require Chat Completions")
        if self.mcp_tools and self.model.provider != "chat_completions":
            raise ValueError("MCP tools require a real-model execution profile")
        if self.browser_reading and not self.browser_origins:
            raise ValueError("Reading tools require browser origins")
        if self.browser_origins and self.model.provider != "chat_completions":
            raise ValueError("Browser tools require Chat Completions")
        refs = self.tool_references
        if any(exports_csv(t) and t.reference not in self.approval_tools for t in self.mcp_tools):
            raise ValueError("CSV export must use the approval workflow")
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
        identities = [(s.source_id, s.manifest.skill_id) for s in self.available_skills]
        if len(set(identities)) != len(identities):
            raise ValueError("Select only one version of each skill")
        if sum(len(s.manifest.content) for s in self.skills) > 32000:
            raise ValueError("Selected skill recipes exceed the context budget")
        if sum(len(s.manifest.content) for s in self.available_skills) > 128000:
            raise ValueError("Skill discovery snapshots exceed the storage budget")
        if any(not set(s.manifest.required_tools) <= set(refs) for s in self.available_skills):
            raise ValueError("A selected skill requires tools not enabled on this Agent")
        return self


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str
