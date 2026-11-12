"""Small, versioned file bridge protocol. All paths are relative to the selected Bot."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WORKSPACE_TOOLS = ("list_files@1", "attach_file@1", "workspace_save@1")
WORKSPACE_FILE_TOOLS = (
    "list_files@1",
    "attach_file@2",
    "workspace_save@1",
    "read_file@1",
    "write_file@1",
)
WORKSPACE_ALIGNED_TOOLS = (
    "list_files@1",
    "attach_file@3",
    "workspace_save@1",
    "read_file@1",
    "write_file@2",
)
WORKSPACE_WRITES = ("workspace_save@1", "write_file@1", "write_file@2")


def workspace_tools(editing=False, aligned=False):
    if aligned:
        return WORKSPACE_ALIGNED_TOOLS
    return WORKSPACE_FILE_TOOLS if editing else WORKSPACE_TOOLS


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


class NamedImportFile(ImportFile):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=280)

    @field_validator("name")
    @classmethod
    def title(cls, value):
        if value is not None and (not value.strip() or not value.isprintable()):
            raise ValueError("Use a printable nonempty artifact title")
        return value


class AlignedImportFile(ImportFile):
    # Bound the raw payload, then normalize like Rakazo's attachment publisher.
    name: str | None = Field(default=None, max_length=4096)
    description: str | None = Field(default=None, max_length=4096)

    @field_validator("name")
    @classmethod
    def title(cls, value):
        value = value.strip()[:255] if value else None
        if value and not value.isprintable():
            raise ValueError("Use a printable artifact title")
        return value or None

    @field_validator("description")
    @classmethod
    def summary(cls, value):
        return value.strip()[:280] or None if value else None


class ReadFile(ImportFile):
    offset: int = Field(default=0, ge=0, le=2 * 1024 * 1024)
    limit: int = Field(default=2000, ge=1, le=2000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class WriteFile(ImportFile):
    content: str = Field(max_length=12000)
    # null means create only; a digest means replace exactly the observed version.
    expected_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    computer_mode: Literal["team", "dedicated"]

    @field_validator("content")
    @classmethod
    def utf8(cls, value):
        value.encode("utf-8")
        if "\0" in value:
            raise ValueError("NUL is not a text character")
        return value


class ListFiles(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(default="", max_length=1024)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return relative_path(value, directory=True)


ARGUMENTS = {
    "workspace_save": SaveFile,
    "attach_file": ImportFile,
    "list_files": ListFiles,
    "read_file": ReadFile,
    "write_file": WriteFile,
}


def argument_model(tool, editing=False, aligned=False):
    if tool == "attach_file" and aligned:
        return AlignedImportFile
    return NamedImportFile if tool == "attach_file" and editing else ARGUMENTS[tool]
