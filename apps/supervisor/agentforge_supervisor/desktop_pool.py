"""Single-host on-demand desktop pool. Only the trusted API can access its UNIX socket."""

import fcntl
import json
import os
import re
import signal
import socketserver
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from agentforge_core.desktop import slot_manifest

from agentforge_supervisor.desktop import docker, running_desktop
from agentforge_supervisor.desktop_slot import execute_workspace


class DesktopPool:
    def __init__(self, directory, image, capacity):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or capacity < 1:
            raise ValueError("Specify a local image ID and a positive desktop limit")
        self.directory = directory
        self.image = image
        self.capacity = capacity
        self.live = {}

    def remove(self, manifest):
        journal = manifest.with_suffix(".pool-generation")
        if resources := self.live.pop(manifest, None):
            resources.close()
        if journal.exists():
            generation = journal.read_text()
            if not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Invalid pool generation")
            name = "agentforge-desktop-" + generation
            if docker("ps", "-aq", "--filter", f"name=^/{name}$"):
                docker("rm", "-f", name)
            manifest.unlink(missing_ok=True)
            journal.unlink()

    def recover(self):
        for journal in self.directory.glob("*.pool-generation"):
            self.remove(journal.with_suffix(".json"))

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
                resources.enter_context(running_desktop(args, bot, self.image, path, generation))
            except BaseException:
                self.remove(path)
                raise
            self.live[path] = resources
        if action == "stop":
            self.remove(path)
        entry = json.loads(path.read_text()) if path in self.live else None
        if action in ("boot", "stop", "status") and not params:
            return {"entry": entry}
        if action not in ("files", "readFile", "downloadFile", "uploadFile"):
            raise ValueError("Unknown desktop operation")
        generation, request = params
        if not entry or entry["generation"] != generation:
            return {"error": "desktop_changed"}
        if (not user) != ("workspaceBot" in request):
            raise ValueError("Wrong workspace scope")
        return execute_workspace(action, request, generation)


def main():
    endpoint = Path(os.environ.get("AGENTFORGE_DESKTOP_POOL", "/slots/pool.sock"))
    if endpoint.parent.stat().st_mode & 0o077:
        raise ValueError("Desktop state directory must be private (0700)")
    os.umask(0o077)
    pool = DesktopPool(
        endpoint.parent,
        os.environ["AGENTFORGE_COMPUTER_IMAGE"],
        int(os.environ.get("AGENTFORGE_DESKTOP_LIMIT", "4")),
    )

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                reply = pool.dispatch(json.loads(self.rfile.readline(14_100_000)))
            except Exception:
                reply = {"error": "Desktop operation failed"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    class Server(socketserver.UnixStreamServer):
        next_check = 0.0

        def service_actions(self):
            if time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + 5
                pool.reap()

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
            pool.recover()
            endpoint.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
