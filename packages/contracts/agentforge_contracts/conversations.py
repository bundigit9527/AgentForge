from datetime import datetime
from uuid import UUID

from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.models import RunStatus
from agentforge_core.skills import SkillReference
from agentforge_core.tools import ToolReferences
from pydantic import BaseModel, ConfigDict, Field


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=10000)
    tools: ToolReferences = ("example_report@1",)
    skills: tuple[SkillReference, ...] = Field(default=(), max_length=8)


class UpdateAgent(CreateAgent):
    expected_version: int = Field(ge=1, strict=True)


class AgentResponse(BaseModel):
    agent_id: str
    name: str
    version: int
    execution_config: ExecutionSnapshot
    created_at: datetime


class AgentPage(BaseModel):
    items: list[AgentResponse]


class CreateThread(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: UUID
    title: str = Field(default="新会话", min_length=1, max_length=200)


class ThreadResponse(BaseModel):
    thread_id: str
    agent_id: str
    agent_version: int
    title: str
    active_run_id: str | None
    created_at: datetime
    updated_at: datetime


class ThreadPage(BaseModel):
    items: list[ThreadResponse]


class MessageResponse(BaseModel):
    message_id: str
    thread_id: str
    run_id: str
    sequence: int
    role: str
    content: str
    run_status: RunStatus
    created_at: datetime


class MessagePage(BaseModel):
    items: list[MessageResponse]
    next_cursor: int


class AttemptResponse(BaseModel):
    attempt_id: str
    run_id: str
    number: int
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


class AttemptPage(BaseModel):
    items: list[AttemptResponse]
