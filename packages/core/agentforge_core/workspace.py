"""Small, versioned file bridge protocol. All paths are relative to the selected Bot."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WORKSPACE_TOOLS = ("list_files@1", "attach_file@1", "workspace_save@1")


def relative_path(value, *, directory=False):
    if directory and value in ("", "/"):
        return ""
    if len(value) > 1024 or any(
        p in ("", ".", "..") or "\\" in p or not p.isprintable() for p in value.split("/")
    ):
        raise ValueError("Use a relative workspace path")
    return value


class SaveFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_id: str
    computer_mode: Literal["team", "dedicated"]

    @field_validator("artifact_id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))


class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return relative_path(value)


class ListFiles(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(default="", max_length=1024)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return relative_path(value, directory=True)


ARGUMENTS = {"workspace_save": SaveFile, "attach_file": ImportFile, "list_files": ListFiles}
