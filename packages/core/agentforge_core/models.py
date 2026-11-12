from dataclasses import dataclass
from enum import StrEnum

from agentforge_core.execution import ContextMessage, ExecutionSnapshot


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


class RunErrorCode(StrEnum):
    WEB_NOT_ALLOWED = "web_not_allowed"
    WEB_UNAVAILABLE = "web_unavailable"
    WEB_TIMEOUT = "web_timeout"
    WEB_CONTENT_UNSUPPORTED = "web_content_unsupported"
    WEB_LIMIT_EXCEEDED = "web_limit_exceeded"
    BROWSER_NOT_ALLOWED = "browser_not_allowed"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    BROWSER_BUSY = "browser_busy"
    BROWSER_PAGE_CHANGED = "browser_page_changed"
    BROWSER_FAILED = "browser_failed"
    WORKSPACE_FILE_CHANGED = "workspace_file_changed"
    WORKSPACE_FILE_UNSUPPORTED = "workspace_file_unsupported"
    WORKSPACE_FILE_MISSING = "workspace_file_missing"
    EXECUTION_FAILED = "execution_failed"
    EFFECT_UNCERTAIN = "effect_uncertain"
    MCP_UNAVAILABLE = "mcp_unavailable"
    MCP_TIMEOUT = "mcp_timeout"
    MCP_INVALID_RESPONSE = "mcp_invalid_response"
    MCP_TOOL_FAILED = "mcp_tool_failed"
    MCP_DEFINITION_CHANGED = "mcp_definition_changed"
    MCP_NOT_ALLOWED = "mcp_not_allowed"
    ACCESS_REVOKED = "access_revoked"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_AUTH_FAILED = "model_auth_failed"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_INVALID_RESPONSE = "model_invalid_response"
    MODEL_REFUSED = "model_refused"
    MODEL_LIMIT_EXCEEDED = "model_limit_exceeded"
    MODEL_CONTEXT_LIMIT_EXCEEDED = "model_context_limit_exceeded"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_INVALID_RESULT = "tool_invalid_result"
    TOOL_CALL_CONFLICT = "tool_call_conflict"
    EXECUTION_CONFIG_MISSING = "execution_config_missing"
    MODEL_CONFIGURATION_MISMATCH = "model_configuration_mismatch"


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    space_id: str
    user_id: str

    def __post_init__(self) -> None:
        if any(not value.strip() or len(value) > 128 for value in vars(self).values()):
            raise ValueError("Scope identifiers must contain 1–128 characters")


@dataclass(frozen=True)
class Run:
    run_id: str
    scope: Scope
    prompt: str
    status: RunStatus
    attempt: int
    result: str | None
    error_code: str | None
    created_at: float
    updated_at: float
    thread_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    execution_config: ExecutionSnapshot | None = None
    context: tuple[ContextMessage, ...] = ()
    input_files: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Claim:
    run: Run
    attempt_id: str


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    run_id: str
    event_type: str
    data: dict
    created_at: float


class RunNotFound(Exception):
    """Missing and unauthorized resources intentionally share one response."""


class RunConflict(Exception):
    """The requested transition is invalid for the current task state."""
