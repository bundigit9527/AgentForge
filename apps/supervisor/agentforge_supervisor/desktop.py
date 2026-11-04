"""Run one opt-in, disposable, operator-assigned development desktop."""

import argparse
import json
import os
import re
import secrets
import signal
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from uuid import UUID, uuid4

PROGRAM = """
set -eu
export HOME=/tmp/home DISPLAY=:1
mkdir -p "$HOME" /tmp/.X11-unix
ln -s /workspace "$HOME/workspace"
cd /workspace
Xvfb :1 -screen 0 1280x800x24 -nolisten tcp -noreset &
until xdpyinfo >/dev/null 2>&1; do sleep .1; done
fluxbox >/tmp/fluxbox.log 2>&1 &
xterm -geometry 90x26+80+60 -title 'AgentForge Desktop' &
vnc() { x11vnc -display :1 -listen 127.0.0.1 -forever -shared -nopw -xkb "$@"; }
vnc -rfbport 5900 -viewonly >/tmp/view.log 2>&1 &
vnc -rfbport 5901 >/tmp/control.log 2>&1 &
until test -s /tmp/targets; do sleep .1; done
exec websockify --web=/usr/share/novnc --token-plugin=TokenFile --token-source=/tmp/targets :6080
"""


def docker(*args, data=None, timeout=30):
    return subprocess.run(
        ["docker", *args],
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
        timeout=timeout,
    ).stdout.strip()


PIPE = """
import os, select, socket
peer = socket.create_connection(("127.0.0.1", 6080), 5)
peer.settimeout(None)
sources = [0, peer]
while True:
    ready, _, _ = select.select(sources, [], [])
    if 0 in ready:
        data = os.read(0, 65536)
        if data:
            peer.sendall(data)
        else:
            peer.shutdown(socket.SHUT_WR)
            sources.remove(0)
    if peer in ready:
        data = peer.recv(65536)
        if not data:
            break
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]
"""


class LoopbackRelay(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, name):
        self.container_name = name
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), RelayConnection)


class RelayConnection(socketserver.StreamRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(timeout=3):
            return
        try:
            self.connection.settimeout(150)
            # One fixed destination inside a network-less container, for HTTP and WebSocket.
            with subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.server.container_name,
                    "python3",
                    "-I",
                    "-S",
                    "-c",
                    PIPE,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=-1,
            ) as child:

                def upload():
                    try:
                        while data := self.connection.recv(65536):
                            child.stdin.write(data)
                            child.stdin.flush()
                    except OSError:
                        pass
                    finally:
                        child.stdin.close()

                threading.Thread(target=upload, daemon=True).start()
                try:
                    while data := child.stdout.read1(65536):
                        self.connection.sendall(data)
                except OSError:
                    pass
                finally:
                    child.terminate()
                    self.connection.close()
        finally:
            self.server.slots.release()


@contextmanager
def running_desktop(args, bot, image, manifest, generation):
    name = "agentforge-desktop-" + generation
    relay = LoopbackRelay(name)
    view, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--init",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=1000,gid=1000",
            "--user",
            "1000:1000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--cpus=1",
            "--memory=768m",
            "--pids-limit=256",
            "-e",
            "LD_PRELOAD=",
            "--log-driver=none",
            "--entrypoint",
            "/usr/bin/timeout",
            image,
            "4h",
            "/bin/sh",
            "-c",
            PROGRAM,
        )
        docker(
            "exec",
            "-i",
            name,
            "sh",
            "-c",
            "umask 077; cat >/tmp/targets.tmp && mv /tmp/targets.tmp /tmp/targets",
            data=f"{view}: 127.0.0.1:5900\n{control}: 127.0.0.1:5901\n",
        )
        port = relay.server_address[1]
        client = build_opener(ProxyHandler({}))
        for _ in range(60):
            try:
                with client.open(f"http://127.0.0.1:{port}/embed.html", timeout=1) as page:
                    if page.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("Desktop did not become ready")
        entry = dict(
            tenant=args.tenant,
            space=args.space,
            user=args.user,
            bot=bot,
            generation=generation,
            port=int(port),
            view=view,
            control=control,
        )
        temp = manifest.with_suffix(".tmp")
        temp.write_text(json.dumps(entry))
        temp.replace(manifest)
        print(f"Desktop ready for bot {bot}", flush=True)
        yield name
    finally:
        manifest.unlink(missing_ok=True)
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        relay.shutdown()
        relay.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tenant", "space", "user", "bot", "manifest"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--on-demand", action="store_true")
    args = parser.parse_args()
    image = os.environ.get("AGENTFORGE_COMPUTER_IMAGE", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        parser.error("Specify a local image ID in AGENTFORGE_COMPUTER_IMAGE")
    bot = str(UUID(args.bot))
    manifest = Path(args.manifest)
    if manifest.parent.stat().st_mode & 0o077:
        parser.error("Choose a fresh manifest inside a private directory (0700)")
    if not args.on_demand and manifest.exists():
        parser.error("Manifest already exists")
    os.umask(0o077)
    if args.on_demand:
        from agentforge_supervisor.desktop_slot import serve

        serve(args, bot, image, manifest)
        return

    def stop(*_):
        raise SystemExit()

    signal.signal(signal.SIGTERM, stop)
    with running_desktop(args, bot, image, manifest, uuid4().hex) as name:
        subprocess.run(["docker", "wait", name], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
