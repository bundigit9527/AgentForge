"""Single-host on-demand desktop pool. Only the trusted API can access its UNIX socket."""

import fcntl
import json
import logging
import os
import re
import signal
import socketserver
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from agentforge_core.desktop import slot_manifest

from agentforge_supervisor.archived_files import browse
from agentforge_supervisor.desktop import docker, running_desktop
from agentforge_supervisor.desktop_slot import execute_workspace
from agentforge_supervisor.workspace_checkpoint import CheckpointError, WorkspaceCheckpoint


class DesktopPool:
    def __init__(self, directory, image, capacity, checkpoints):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or capacity < 1:
            raise ValueError("Specify a local image ID and a positive desktop limit")
        self.directory = directory
        self.image = image
        self.capacity = capacity
        self.live = {}
        self.checkpoints = Path(checkpoints)
        self.checkpoints.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.checkpoints.stat().st_mode & 0o077 or self.checkpoints == self.directory:
            raise ValueError("Choose a separate private workspace checkpoint directory")
        self.last_save = {}
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-save")
        self.pending = None
        self.baselines = {}
        self.prepared = {}

    def remove(self, manifest, *, saved=False):
        self.finish_checkpoint(manifest)
        journal = manifest.with_suffix(".pool-generation")
        if journal.exists():
            generation = journal.read_text()
            if not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Invalid pool generation")
            name = "agentforge-desktop-" + generation
            checkpoint = WorkspaceCheckpoint(self.checkpoints, manifest, generation, self.image)
            if manifest.exists() and json.loads(manifest.read_text())["generation"] != generation:
                raise RuntimeError("Workspace generation mismatch")
            if (
                not saved
                and manifest.exists()
                and docker("ps", "-q", "--filter", f"name=^/{name}$")
            ):
                checkpoint.save(stopping=True)
            if resources := self.live.pop(manifest, None):
                resources.close()
            if docker("ps", "-aq", "--filter", f"name=^/{name}$"):
                docker("rm", "-f", name)
            checkpoint.discard_runtime()
            self.last_save.pop(manifest, None)
            self.baselines.pop(manifest, None)
            self.prepared.pop(manifest, None)
            manifest.unlink(missing_ok=True)
            journal.unlink()

    def recover(self):
        errors = []
        for journal in self.directory.glob("*.pool-generation"):
            try:
                self.remove(journal.with_suffix(".json"))
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Could not recover all desktop slots", errors)

    def reap(self):
        if not self.live:
            return
        names = set(
            docker(
                "ps", "--filter", "name=agentforge-desktop-", "--format", "{{.Names}}"
            ).splitlines()
        )
        for manifest in list(self.live):
            name = "agentforge-desktop-" + manifest.with_suffix(".pool-generation").read_text()
            if name not in names:
                self.remove(manifest)

    def save(self, path, generation):
        WorkspaceCheckpoint(self.checkpoints, path, generation, self.image).save()
        self.last_save[path] = time.monotonic()

    def finish_checkpoint(self, path=None):
        if self.pending is None:
            return
        active, future = self.pending
        if path != active and not future.done():
            return
        try:
            future.result()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            logging.exception("Periodic checkpoint failed for %s", active.stem)
        self.pending = None
        self.last_save[active] = time.monotonic()

    def periodic(self):
        self.finish_checkpoint()
        for path, (generation, since) in list(self.prepared.items()):
            if time.monotonic() - since >= 15:
                self.cancel_suspend(path, generation)
        candidates = self.last_save.keys() - self.prepared.keys()
        if self.pending or not candidates:
            return
        path = min(candidates, key=self.last_save.get)
        if time.monotonic() - self.last_save[path] >= 30:
            generation = path.with_suffix(".pool-generation").read_text()
            checkpoint = WorkspaceCheckpoint(self.checkpoints, path, generation, self.image)
            self.pending = path, self.writer.submit(checkpoint.save)

    def processes(self, generation):
        lines = docker(
            "top", "agentforge-desktop-" + generation, "-eo", "pid,lstart,comm"
        ).splitlines()
        if len(lines) < 2:
            raise RuntimeError("No desktop process list")
        return {" ".join(line.split()) for line in lines[1:]}

    def cancel_suspend(self, path, generation):
        name = "agentforge-desktop-" + generation
        if docker("inspect", "-f", "{{.State.Paused}}", name) == "true":
            docker("unpause", name)
        self.prepared.pop(path, None)

    def idle_action(self, path, generation, action):
        baseline = self.baselines.get(path)
        if action == "prepareSuspend":
            self.finish_checkpoint(path)
            if not baseline or path in self.prepared or self.processes(generation) - baseline:
                return {"ready": False}
            # Freeze before the final process check. Newly spawned jobs keep their desktop.
            docker("pause", "agentforge-desktop-" + generation)
            try:
                if self.processes(generation) - baseline:
                    self.cancel_suspend(path, generation)
                    return {"ready": False}
                checkpoint = WorkspaceCheckpoint(self.checkpoints, path, generation, self.image)
                checkpoint.save(stopping=True)
            except Exception:
                self.cancel_suspend(path, generation)
                raise
            self.prepared[path] = generation, time.monotonic()
            return {"ready": True}
        if path not in self.prepared or self.prepared[path][0] != generation:
            return {"ready": False}
        if action == "cancelSuspend":
            self.cancel_suspend(path, generation)
        else:
            path.with_suffix(".suspended").touch()
            self.remove(path, saved=True)
        return {"ready": True}

    def dispatch(self, message):
        action, owner, *params = message
        tenant, space, user, bot = owner
        if not tenant or not space or not all(isinstance(x, str) and len(x) <= 128 for x in owner):
            raise ValueError("Invalid scope")
        if str(UUID(bot)) != bot or (not user) != (UUID(bot).int == 0):
            raise ValueError("Invalid computer binding")
        path = slot_manifest(self.directory, owner)
        if action == "boot" and path not in self.live:
            self.reap()
            if len(self.live) >= self.capacity:
                return {"error": "pool_full"}
            self.remove(path)
            generation = uuid4().hex
            tmp = path.with_suffix(".pending")
            tmp.write_text(generation)
            tmp.replace(path.with_suffix(".pool-generation"))
            args = SimpleNamespace(tenant=tenant, space=space, user=user)
            resources = ExitStack()
            try:
                checkpoint = WorkspaceCheckpoint(self.checkpoints, path, generation, self.image)
                resources.enter_context(
                    running_desktop(args, bot, self.image, path, generation, checkpoint)
                )
            except BaseException:
                self.remove(path)
                raise
            try:
                self.baselines[path] = self.processes(generation)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                logging.exception("Could not establish idle baseline for %s", path.stem)
            path.with_suffix(".suspended").unlink(missing_ok=True)
            self.live[path] = resources
            self.last_save[path] = time.monotonic()
        if action == "stop":
            self.remove(path)
            path.with_suffix(".suspended").unlink(missing_ok=True)
        entry = json.loads(path.read_text()) if path in self.live else None
        if action in ("boot", "stop", "status") and not params:
            return {
                "entry": entry,
                "state": "suspended" if path.with_suffix(".suspended").exists() else "stopped",
            }
        if action in ("prepareSuspend", "commitSuspend", "cancelSuspend"):
            if entry and params == [entry["generation"]]:
                return self.idle_action(path, entry["generation"], action)
            return {"ready": False}
        if action not in ("files", "readFile", "downloadFile", "uploadFile"):
            raise ValueError("Unknown desktop operation")
        generation, request = params
        if (not user) != ("workspaceBot" in request):
            raise ValueError("Wrong workspace scope")
        if entry is None and generation is None and action in ("files", "readFile"):
            try:
                return {"result": browse(self.checkpoints / (path.stem + ".tar"), action, request)}
            except FileNotFoundError:
                return {"error": "file_missing"}
            except (ValueError, OSError, tarfile.TarError):
                return {"error": "file_invalid"}
        if not entry or entry["generation"] != generation:
            return {"error": "desktop_changed"}
        self.finish_checkpoint(path)
        result = execute_workspace(action, request, generation)
        if action == "uploadFile" and result.get("result", {}).get("ok"):
            self.save(path, generation)
        return result


def main():
    endpoint = Path(os.environ.get("AGENTFORGE_DESKTOP_POOL", "/slots/pool.sock"))
    if endpoint.parent.stat().st_mode & 0o077:
        raise ValueError("Desktop state directory must be private (0700)")
    os.umask(0o077)
    pool = DesktopPool(
        endpoint.parent,
        os.environ["AGENTFORGE_COMPUTER_IMAGE"],
        int(os.environ.get("AGENTFORGE_DESKTOP_LIMIT", "4")),
        os.environ.get("AGENTFORGE_WORKSPACE_CHECKPOINTS", str(endpoint.parent / "checkpoints")),
    )

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                reply = pool.dispatch(json.loads(self.rfile.readline(14_100_000)))
            except CheckpointError:
                logging.exception("Could not save desktop workspace")
                reply = {"error": "checkpoint_failed"}
            except Exception:
                reply = {"error": "Desktop operation failed"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    class Server(socketserver.UnixStreamServer):
        next_check = 0.0

        def service_actions(self):
            if time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + 5
                pool.reap()
                pool.periodic()

    def stop(*_):
        raise SystemExit()

    with open(endpoint.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pool.recover()
        endpoint.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, stop)
        try:
            with Server(str(endpoint), Handler) as server:
                server.serve_forever()
        finally:
            pool.writer.shutdown(wait=True)
            pool.recover()
            endpoint.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
