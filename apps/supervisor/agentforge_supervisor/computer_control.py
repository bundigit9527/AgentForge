"""Fixed program injected into the selected offline desktop, never run on the host."""

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
KEYS = {
    "Return",
    "Tab",
    "Escape",
    "BackSpace",
    "Delete",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "Page_Up",
    "Page_Down",
    "ctrl+a",
    "ctrl+c",
}
ENV = {**os.environ, "DISPLAY": ":1", "HOME": "/tmp/home"}


def capture():
    with tempfile.TemporaryDirectory(prefix="af-observe-") as folder:
        target = Path(folder) / "screen.png"
        subprocess.run(
            [
                "import",
                "-display",
                ":1",
                "-window",
                "root",
                "-depth",
                "8",
                "-strip",
                "-define",
                "png:exclude-chunks=date,time",
                "-crop",
                "1280x776+0+0",
                "+repage",
                "PNG:" + str(target),
            ],
            env=ENV,
            check=True,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if target.stat().st_size > MAX_BYTES:
            raise ValueError("Screen exceeds image budget")
        raw = target.read_bytes()
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Screen is not PNG")
    width, height = struct.unpack(">II", raw[16:24])
    if not (1 <= width <= 1920 and 1 <= height <= 1200):
        raise ValueError("Unsupported screen dimensions")
    return dict(
        mime_type="image/png",
        width=width,
        height=height,
        sha256=hashlib.sha256(raw).hexdigest(),
        data=base64.b64encode(raw).decode(),
    )


def commands(actions, width, height):
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        raise ValueError("Invalid action batch")
    result = []
    for action in actions:
        kind = action.get("kind")
        if kind == "click" and set(action) <= {"kind", "x", "y", "button"}:
            x, y, button = action["x"], action["y"], action.get("button", "left")
            if (
                type(x) is not int
                or type(y) is not int
                or not 0 <= x < width
                or not 0 <= y < height
                or button not in {"left", "right"}
            ):
                raise ValueError("Invalid click")
            result.append(
                (
                    [
                        "xdotool",
                        "mousemove",
                        "--sync",
                        str(x),
                        str(y),
                        "click",
                        "1" if button == "left" else "3",
                    ],
                    None,
                )
            )
        elif kind == "key" and set(action) == {"kind", "key"} and action["key"] in KEYS:
            result.append((["xdotool", "key", "--clearmodifiers", action["key"]], None))
        elif kind == "type" and set(action) == {"kind", "text"}:
            text = action["text"]
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= 1000
                or any(ord(c) < 32 or ord(c) == 127 for c in text)
            ):
                raise ValueError("Invalid text")
            result.append(
                (["xdotool", "type", "--clearmodifiers", "--delay", "1", "--file", "-"], text)
            )
        elif kind == "scroll" and set(action) <= {"kind", "direction", "clicks"}:
            clicks = action.get("clicks", 3)
            if (
                action["direction"] not in {"up", "down"}
                or type(clicks) is not int
                or not 1 <= clicks <= 8
            ):
                raise ValueError("Invalid scroll")
            result.append(
                (
                    [
                        "xdotool",
                        "click",
                        "--repeat",
                        str(clicks),
                        "--delay",
                        "50",
                        "4" if action["direction"] == "up" else "5",
                    ],
                    None,
                )
            )
        else:
            raise ValueError("Unsupported action")
    return result


def operate(action, request):
    completed = 0
    if action == "observeComputer":
        if set(request) - {"workspaceBot"}:
            raise ValueError("Invalid observation")
        return {"result": dict(completed=0, image=capture())}
    if action == "actComputer":
        if set(request) - {"workspaceBot", "screen_sha256", "actions"}:
            raise ValueError("Invalid action")
        image = capture()
        batch = commands(request["actions"], image["width"], image["height"])
        if request["screen_sha256"] != image["sha256"]:
            return {"error": "computer_screen_changed"}
        for argv, text in batch:
            subprocess.run(
                argv,
                input=text,
                text=True,
                env=ENV,
                timeout=10,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            completed += 1
    elif action == "launchApp":
        if set(request) - {"workspaceBot", "application"}:
            raise ValueError("Invalid launch")
        apps = {
            "browser": ["/usr/local/bin/rakazo-browser", "about:blank"],
            "calculator": ["xcalc"],
        }
        argv = apps[request["application"]]
        if not shutil.which(argv[0]):
            return {"error": "computer_application_missing"}
        subprocess.Popen(
            argv,
            env=ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        completed = 1
    else:
        raise ValueError("Unsupported computer operation")
    time.sleep(0.5)
    # A screenshot failure after input dispatch is an unknown result; never retry it.
    return {"result": dict(completed=completed, image=capture())}


if __name__ == "__main__":
    try:
        action, request = json.loads(sys.stdin.read(30000))
        print(json.dumps(operate(action, request)))
    except Exception:
        print(json.dumps({"error": "computer_operation_failed"}))
