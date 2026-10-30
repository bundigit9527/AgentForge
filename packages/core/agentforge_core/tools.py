import hashlib
import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, Field

ToolReference = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,5}$")]


def unique_tool_names(refs: tuple[str, ...]) -> tuple[str, ...]:
    if len({ref.split("@")[0] for ref in refs}) != len(refs):
        raise ValueError("Choose one version of each tool")
    return refs


ToolReferences = Annotated[
    tuple[ToolReference, ...], Field(max_length=16), AfterValidator(unique_tool_names)
]


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str
    name: str
    version: int
    arguments_json: str

    @property
    def arguments(self) -> dict:
        return json.loads(self.arguments_json)

    @property
    def arguments_hash(self) -> str:
        return hashlib.sha256(self.arguments_json.encode()).hexdigest()


@dataclass(frozen=True)
class ToolTicket:
    tool_call_id: str
    cached: bool = False
    content: str | None = None
