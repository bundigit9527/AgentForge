"""Bounded desktop images and actions; credentials and paths are never model inputs."""

import base64
import hashlib
import struct
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SCREEN_BYTES = 2 * 1024 * 1024
COMPUTER_TOOLS = ("computer_observe@1", "computer_act@1", "launch_app@1")


class ScreenImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    mime_type: Literal["image/png"] = "image/png"
    width: int = Field(ge=1, le=1920)
    height: int = Field(ge=1, le=1200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data: str = Field(max_length=(MAX_SCREEN_BYTES + 2) // 3 * 4, repr=False)

    @model_validator(mode="after")
    def valid(self):
        raw = base64.b64decode(self.data, validate=True)
        if (
            len(raw) > MAX_SCREEN_BYTES
            or len(raw) < 33
            or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or raw[12:16] != b"IHDR"
            or struct.unpack(">II", raw[16:24]) != (self.width, self.height)
            or hashlib.sha256(raw).hexdigest() != self.sha256
        ):
            raise ValueError("Invalid bounded desktop image")
        return self

    def metadata(self):
        return self.model_dump(exclude={"data"})


class ObserveComputer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Click(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["click"]
    x: int = Field(ge=0, lt=1920)
    y: int = Field(ge=0, lt=1200)
    button: Literal["left", "right"] = "left"


class Key(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["key"]
    key: Literal[
        "Return",
        "Tab",
        "Escape",
        "BackSpace",
        "Delete",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "Page_Up",
        "Page_Down",
        "ctrl+a",
        "ctrl+c",
    ]


class TypeText(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["type"]
    text: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid(self):
        if any(ord(c) < 32 or ord(c) == 127 for c in self.text):
            raise ValueError("Use key actions for control characters")
        return self


class Scroll(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["scroll"]
    direction: Literal["up", "down"]
    clicks: int = Field(default=3, ge=1, le=8)


class ActComputer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    computer_mode: Literal["team", "dedicated"]
    screen_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actions: list[Annotated[Click | Key | TypeText | Scroll, Field(discriminator="kind")]] = Field(
        min_length=1, max_length=8
    )


class LaunchApp(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    computer_mode: Literal["team", "dedicated"]
    application: Literal["browser", "calculator"]
