"""Fixed program executed inside the desktop. All paths are relative to its workspace mount."""

import base64
import hashlib
import json
import os
import secrets
import stat
import sys
from contextlib import ExitStack
from uuid import UUID

MAX_FILE = 10 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def path_parts(action, path):
    parts = [] if path in ("", "/") and action == "files" else path.split("/")
    if len(path) > 1024 or any(
        part in ("", ".", "..") or "\\" in part or not part.isprintable() for part in parts
    ):
        raise ValueError("Invalid workspace path")
    return parts


def operate(action, request, root="/workspace"):
    path = request["path"]
    parts = path_parts(action, path)
    if action == "saveArtifact":
        expected = ["artifacts", str(UUID(request["artifactId"])), request["name"]]
        if parts != expected or len(parts) != 3:
            raise ValueError("Invalid artifact destination")
    with ExitStack() as resources:
        parent = os.open(root, DIR)
        resources.callback(os.close, parent)
        if bot := request.get("workspaceBot"):
            name = str(UUID(bot))
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            parent = os.open(name, DIR, dir_fd=parent)
            resources.callback(os.close, parent)
        for part in parts if action == "files" else parts[:-1]:
            if action == "saveArtifact":
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = os.open(part, DIR, dir_fd=parent)
            resources.callback(os.close, parent)
        if action == "files":
            results = []
            with os.scandir(parent) as children:
                for index, item in enumerate(children):
                    if index >= 1024:
                        raise ValueError("Too many entries")
                    if not item.name.isprintable() or "\\" in item.name:
                        continue
                    try:
                        meta = item.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    directory = stat.S_ISDIR(meta.st_mode)
                    if directory or stat.S_ISREG(meta.st_mode):
                        results.append(
                            dict(
                                path="/".join([*parts, item.name]),
                                kind="dir" if directory else "file",
                                size=0 if directory else meta.st_size,
                            )
                        )
            return sorted(results, key=lambda item: item["path"])
        name = parts[-1]
        if action == "saveArtifact":
            data = base64.b64decode(request["contentBase64"], validate=True)
            digest = hashlib.sha256(data).hexdigest()
            if not 0 < len(data) <= MAX_FILE or request["sha256"] != digest:
                raise ValueError("Invalid artifact bytes")
            tmp = ".af-artifact-" + secrets.token_hex(16)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    os.link(tmp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    existing = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    with os.fdopen(existing, "rb") as src:
                        meta = os.fstat(src.fileno())
                        if (
                            not stat.S_ISREG(meta.st_mode)
                            or meta.st_size != len(data)
                            or src.read(MAX_FILE + 1) != data
                        ):
                            raise ValueError(
                                "Desktop copy already changed; refusing overwrite"
                            ) from None
                os.fsync(parent)
            finally:
                os.unlink(tmp, dir_fd=parent)
            return {"path": path, "size": len(data), "sha256": digest}
        if action == "uploadFile":
            data = base64.b64decode(request["contentBase64"], validate=True)
            if len(data) > MAX_FILE:
                raise ValueError("File too large")
            tmp = ".af-upload-" + secrets.token_hex(16)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except FileNotFoundError:
                    pass
            return {"ok": True}
        if action not in ("downloadFile", "readFile"):
            raise ValueError("Invalid operation")
        limit = MAX_TEXT if action == "readFile" else MAX_FILE
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=parent)
        resources.callback(os.close, fd)
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise ValueError("Unsupported file")
        with open(fd, "rb", closefd=False) as src:
            raw = src.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("File too large")
        if action == "readFile":
            return {"path": path, "content": raw.decode("utf-8")}
        return {"path": path, "contentBase64": base64.b64encode(raw).decode("ascii")}


if __name__ == "__main__":
    try:
        action, request = json.load(sys.stdin)
        reply = {"result": operate(action, request)}
    except FileNotFoundError:
        reply = {"error": "file_missing"}
    except (ValueError, OSError):
        reply = {"error": "file_invalid"}
    print(json.dumps(reply, ensure_ascii=True))
