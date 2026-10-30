from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ToolDescription(BaseModel):
    reference: str
    name: str
    version: int
    description: str
    effect: Literal["pure"]
    input_schema: dict
    output_schema: dict


class ToolCatalog(BaseModel):
    items: list[ToolDescription]


class ToolCallResponse(BaseModel):
    tool_call_id: str
    run_id: str
    attempt_id: str
    sequence: int
    call_id: str
    name: str
    version: int
    arguments: dict
    status: Literal["running", "succeeded", "failed", "abandoned"]
    output: dict | None
    content: str | None
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


class ToolCallPage(BaseModel):
    items: list[ToolCallResponse]
    next_cursor: int
