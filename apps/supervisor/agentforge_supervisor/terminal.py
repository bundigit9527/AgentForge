"""Loopback-only WebSocket relay for original Rakazo xterm sessions."""

import os
import re
import select
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

GUEST = Path(__file__).with_name("terminal_pty.py")


@contextmanager
def terminal_relay(generation):
    name = "agentforge-desktop-" + generation
    origin = os.environ.get("AGENTFORGE_TERMINAL_API_ORIGIN", "http://127.0.0.1:8000")
    url = origin.rstrip("/") + "/api/internal/terminal-access"
    sessions = threading.BoundedSemaphore(8)

    def handle(ws):
        match = re.fullmatch(r"/terminal/([A-Za-z0-9_-]{43})", ws.request.path)
        if not match or ws.subprotocol != "binary" or not sessions.acquire(blocking=False):
            ws.close(1008)
            return
        try:
            with httpx.Client(timeout=2, trust_env=False) as api:

                def authorize():
                    res = api.post(url, json={"key": match[1], "generation": generation})
                    res.raise_for_status()
                    return res.json()["workspaceBot"]

                bot = authorize()
                run_shell(ws, name, bot, authorize)
        except (ConnectionClosed, httpx.HTTPError, OSError, ValueError):
            ws.close(1008)
        finally:
            sessions.release()

    with serve(
        handle,
        "127.0.0.1",
        0,
        subprotocols=["binary"],
        compression=None,
        max_size=65541,
        close_timeout=1,
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.socket.getsockname()[1]
        finally:
            server.shutdown()
            thread.join()


def run_shell(ws, name, bot, authorize):
    with subprocess.Popen(
        ["docker", "exec", "-i", name, "python3", "-I", "-S", "-c", GUEST.read_text(), bot or ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    ) as child:

        def output():
            try:
                while chunk := child.stdout.read(65536):
                    ws.send(chunk)
            except (ConnectionClosed, OSError):
                pass
            finally:
                ws.close()

        threading.Thread(target=output, daemon=True).start()
        fd = child.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            last_check = 0.0
            while child.poll() is None:
                try:
                    frame = ws.recv(timeout=0.4)
                except TimeoutError:
                    if time.monotonic() - last_check < 0.8:
                        continue
                    frame = None
                authorize()
                last_check = time.monotonic()
                if frame is None:
                    continue
                if not isinstance(frame, bytes):
                    raise ValueError("Expected binary terminal data")
                view = memoryview(frame)
                while view:
                    if not select.select([], [fd], [], 0.5)[1]:
                        raise OSError("Terminal input stalled")
                    view = view[os.write(fd, view) :]
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
