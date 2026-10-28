from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


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
