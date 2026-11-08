"""File limits and attachment metadata shared by the Bot chat contract and worker."""

from typing import Literal

TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "text/html", "application/json"})
FILE_TYPES = TEXT_TYPES | {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
MAX_FILE = 10 * 1024 * 1024
ResultMime = Literal["text/plain", "text/markdown", "text/csv", "application/json"]


def filename(value):
    if (
        not value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not value.isprintable()
    ):
        raise ValueError("Invalid attachment name")
    return value
