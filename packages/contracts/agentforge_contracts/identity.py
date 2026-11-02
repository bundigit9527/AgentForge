from typing import Literal

from agentforge_core.identity import Role
from pydantic import BaseModel, ConfigDict, Field


class SetMember(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str = Field(min_length=1, max_length=128)
    role: Role
    active: bool = Field(default=True, strict=True)


class SetVisibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: Literal["private", "space"]
