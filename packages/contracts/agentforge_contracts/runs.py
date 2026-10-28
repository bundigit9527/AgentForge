from datetime import UTC, datetime

from agentforge_core.models import Run, RunEvent, RunStatus
from pydantic import BaseModel, ConfigDict, Field


class CreateRun(BaseModel):
    # Identity is resolved by the server, never supplied in the task body.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=10000)


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    attempt: int
    result: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_run(cls, run: Run) -> "RunResponse":
        return cls(
            run_id=run.run_id,
            status=run.status,
            attempt=run.attempt,
            result=run.result,
            error_code=run.error_code,
            created_at=datetime.fromtimestamp(run.created_at, UTC),
            updated_at=datetime.fromtimestamp(run.updated_at, UTC),
        )


class EventResponse(BaseModel):
    sequence: int
    event_type: str
    data: dict
    created_at: datetime

    @classmethod
    def from_event(cls, event: RunEvent) -> "EventResponse":
        return cls(
            sequence=event.sequence,
            event_type=event.event_type,
            data=event.data,
            created_at=datetime.fromtimestamp(event.created_at, UTC),
        )


class EventPage(BaseModel):
    items: list[EventResponse]
    next_cursor: int
