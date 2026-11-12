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


class WorkspaceFileError(ValueError):
    """A definite, pre-write refusal that can safely be returned to the Harness."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def text_operation(action, request, parent, name):
    # API serializes Agent/manual operations; flock also serializes cooperating writers.
    import fcntl

    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            if action == "readText" or request["expected_sha256"] is not None:
                raise WorkspaceFileError("workspace_file_missing") from None
        if fd is not None:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_TEXT:
                raise WorkspaceFileError("workspace_file_unsupported")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with open(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_TEXT + 1)
            if len(raw) > MAX_TEXT:
                raise WorkspaceFileError("workspace_file_unsupported")
            digest = hashlib.sha256(raw).hexdigest()
            expected = request.get("expected_sha256")
            if (action == "writeText" or expected is not None) and expected != digest:
                raise WorkspaceFileError("workspace_file_changed")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceFileError("workspace_file_unsupported") from None
            if "\0" in text:
                raise WorkspaceFileError("workspace_file_unsupported")
        if action == "readText":
            offset, limit = request.get("offset", 0), request.get("limit", 2000)
            if (
                type(offset) is not int
                or type(limit) is not int
                or not 0 <= offset <= len(text)
                or not 1 <= limit <= 2000
            ):
                raise WorkspaceFileError("workspace_file_unsupported")
            end = min(len(text), offset + limit)
            return dict(
                path=request["path"],
                content=text[offset:end],
                size=len(raw),
                sha256=digest,
                offset=offset,
                next_offset=end if end < len(text) else None,
                truncated=end < len(text),
            )
        content = request["content"]
        if not isinstance(content, str) or len(content) > 12000 or "\0" in content:
            raise WorkspaceFileError("workspace_file_unsupported")
        data = content.encode("utf-8")
        tmp = ".af-text-" + secrets.token_hex(16)
        out_fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
        try:
            with os.fdopen(out_fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if fd is None:
                try:
                    os.link(tmp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    raise WorkspaceFileError("workspace_file_changed") from None
            else:
                # Reject a path replaced or modified while preparing the new bytes.
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                ) != (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns):
                    raise WorkspaceFileError("workspace_file_changed")
                os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass
        return dict(path=request["path"], size=len(data), sha256=hashlib.sha256(data).hexdigest())
    finally:
        if fd is not None:
            os.close(fd)


def operate(action, request, root="/workspace"):
    path = request["path"]
    parts = path_parts(action, path)
    create_parents = action == "writeTextV2" and request.get("expected_sha256") is None
    if action == "writeTextV2":
        # Validate before creating anything, including the Team Bot directory.
        content = request["content"]
        if not isinstance(content, str) or len(content) > 12000 or "\0" in content:
            raise WorkspaceFileError("workspace_file_unsupported")
        content.encode("utf-8")
        action = "writeText"
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
            if action == "saveArtifact" or create_parents:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            try:
                parent = os.open(part, DIR, dir_fd=parent)
            except FileNotFoundError:
                if action in {"readText", "writeText"}:
                    raise WorkspaceFileError("workspace_file_missing") from None
                raise
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
        if action in {"readText", "writeText"}:
            return text_operation(action, request, parent, name)
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
    except WorkspaceFileError as error:
        reply = {"error": error.code}
    except FileNotFoundError:
        reply = {"error": "file_missing"}
    except (ValueError, OSError):
        reply = {"error": "file_invalid"}
    print(json.dumps(reply, ensure_ascii=True))
