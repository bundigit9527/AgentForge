"""Read the latest private workspace checkpoint without extracting it or starting a desktop."""

import os
import tarfile
from uuid import UUID

from agentforge_supervisor.workspace_checkpoint import MAX_ARCHIVE
from agentforge_supervisor.workspace_files import MAX_TEXT, path_parts


def browse(archive, action, request):
    path = request["path"]
    parts = path_parts(action, path)
    listing = action == "files"
    prefix = [str(UUID(request["workspaceBot"]))] if "workspaceBot" in request else []
    target = "/".join(prefix + parts)
    parent_names = {"/".join((prefix + parts)[:i]) for i in range(1, len(prefix + parts))}
    if not archive.exists() and listing and not parts:
        return []
    with archive.open("rb") as raw, tarfile.open(fileobj=raw, mode="r:") as tar:
        if os.fstat(raw.fileno()).st_size > MAX_ARCHIVE:
            raise ValueError("Oversized archive")
        entries = []
        found = None
        for i, item in enumerate(tar):
            if i >= 65536:
                raise ValueError("Too many archive entries")
            name = item.name.removeprefix("./").rstrip("/")
            if name in parent_names and not item.isdir():
                raise ValueError("Invalid workspace directory")
            if name == target:
                found = item
            parent, _, child = name.rpartition("/")
            if (
                listing
                and parent == target
                and child not in ("", ".", "..")
                and (item.isdir() or item.isfile())
                and child.isprintable()
                and "\\" not in child
            ):
                entries.append(
                    dict(
                        path="/".join(parts + [child]),
                        kind="dir" if item.isdir() else "file",
                        size=0 if item.isdir() else item.size,
                    )
                )
                if len(entries) > 1024:
                    raise ValueError("Too many entries")
        if listing:
            if found and not found.isdir():
                raise ValueError("Expected directory")
            if parts and found is None:
                raise FileNotFoundError()
            return sorted(entries, key=lambda item: item["path"])
        if found is None:
            raise FileNotFoundError()
        if not found.isfile() or found.size > MAX_TEXT:
            raise ValueError("Unsupported text preview")
        with tar.extractfile(found) as member:
            return {"path": path, "content": member.read(MAX_TEXT + 1).decode("utf-8")}
