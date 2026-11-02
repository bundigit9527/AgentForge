from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RpcInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Empty(RpcInput):
    pass


class Target(RpcInput):
    botId: UUID


class Bootstrap(RpcInput):
    botId: UUID | None = None


class History(Target):
    before: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    includePeerRuns: Literal[False] = False
    includePeerReceipts: Literal[False] = False


class Subscribe(Target):
    cursor: int = Field(ge=-1, le=2**53 - 1, strict=True)


class Send(Target):
    text: str = Field(min_length=1, max_length=10000)
    clientNonce: str | None = Field(default=None, min_length=1, max_length=200)
    artifactIds: list = Field(default_factory=list, max_length=0)
    mentions: list = Field(default_factory=list, max_length=0)


class CreateBot(RpcInput):
    name: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=10000)
    notifyOnFinish: bool = Field(default=False, strict=True)
    color: str | None = Field(default=None, max_length=200)
    computerMode: Literal["team"] = "team"
    spawnKey: str | None = Field(default=None, min_length=1, max_length=120)


class UpdateBot(Target):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    instructions: str | None = Field(default=None, max_length=10000)
    notifyOnFinish: bool | None = Field(default=None, strict=True)
    color: str | None = Field(default=None, max_length=200)
    pinned: bool | None = Field(default=None, strict=True)


class Login(RpcInput):
    token: str = Field(min_length=40, max_length=256, repr=False)
