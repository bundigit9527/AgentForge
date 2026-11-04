"""Local socket serving the lifecycle of one operator-authorized desktop slot."""

import fcntl
import json
import os
import re
import signal
import socketserver
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from agentforge_supervisor.desktop import docker, running_desktop


def execute_workspace(action, request, generation):
    program = Path(__file__).with_name("workspace_files.py").read_text()
    result = docker(
        "exec",
        "-i",
        "agentforge-desktop-" + generation,
        "python3",
        "-I",
        "-S",
        "-c",
        program,
        data=json.dumps([action, request]),
    )
    return json.loads(result)


def serve(args, bot, image, manifest):
    identity = [args.tenant, args.space, args.user, bot]
    endpoint = str(manifest) + ".sock"
    journal = manifest.with_suffix(manifest.suffix + ".generation")
    resources = ExitStack()

    def recover():
        # Only remove a container whose generated name we saved before provisioning.
        if journal.exists():
            generation = journal.read_text()
            if not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Invalid desktop lifecycle journal")
            name = "agentforge-desktop-" + generation
            if docker("ps", "-aq", "--filter", f"name=^/{name}$"):
                docker("rm", "-f", name)
            manifest.unlink(missing_ok=True)
            journal.unlink()
        elif manifest.exists():
            raise ValueError("Use a separate manifest for this managed desktop")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(3)
            try:
                action, owner, *params = json.loads(self.rfile.readline(14_100_000))
                if owner != identity:
                    raise ValueError("Wrong desktop slot")
                if action in ("files", "readFile", "downloadFile", "uploadFile"):
                    reply = self.workspace(action, params)
                elif not params and action in ("status", "boot", "stop"):
                    reply = self.lifecycle(action)
                else:
                    raise ValueError("Unrecognized desktop request")
            except Exception:
                reply = {"error": "Desktop lifecycle request failed"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

        def workspace(self, action, params):
            generation, request = params
            if not manifest.exists() or journal.read_text() != generation:
                return {"error": "desktop_changed"}
            return execute_workspace(action, request, generation)

        def lifecycle(self, action):
            if action == "stop":
                resources.close()
                recover()
            elif action == "boot" and not manifest.exists():
                recover()
                generation = uuid4().hex
                pending = journal.with_suffix(".pending")
                pending.write_text(generation)
                pending.replace(journal)
                resources.enter_context(running_desktop(args, bot, image, manifest, generation))
            entry = json.loads(manifest.read_text()) if manifest.exists() else None
            return {"entry": entry}

    class Server(socketserver.UnixStreamServer):
        next_check = 0.0

        def service_actions(self):
            if time.monotonic() < self.next_check or not manifest.exists():
                return
            self.next_check = time.monotonic() + 5
            name = "agentforge-desktop-" + journal.read_text()
            if not docker("ps", "-q", "--filter", f"name=^/{name}$"):
                resources.close()
                recover()

    def stop(*_):
        raise SystemExit()

    with open(endpoint + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover()
        if os.path.exists(endpoint):
            os.unlink(endpoint)
        signal.signal(signal.SIGTERM, stop)
        try:
            with Server(endpoint, Handler) as server:
                server.serve_forever()
        finally:
            resources.close()
            recover()
            if os.path.exists(endpoint):
                os.unlink(endpoint)
