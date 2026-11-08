"""Arbitrary Python runs only in disposable, offline, resource-limited containers."""

import re
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from agentforge_core.python_execution import PythonJob, PythonResponse

GUEST = Path(__file__).with_name("python_guest.py").read_text()


class PythonComputer:
    def __init__(self, image):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Configure a pinned AGENTFORGE_PYTHON_IMAGE")
        self.image = image
        self.slots = threading.BoundedSemaphore(2)

    def execute(self, payload, stopped):
        job = PythonJob.model_validate(payload)
        if job.image != self.image:
            raise ValueError("Python image mismatch")
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("Python computer busy")
        name = "agentforge-python-" + uuid4().hex
        child = None
        try:
            command = [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--interactive",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--user=65534:65534",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--cpus=1",
                "--memory=384m",
                "--memory-swap=384m",
                "--pids-limit=32",
                "--tmpfs",
                "/workspace:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=65534,gid=65534",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
                "--workdir=/workspace",
                "--env=HOME=/tmp",
                "--log-driver=none",
                "--entrypoint=python3",
                self.image,
                "-I",
                "-c",
                GUEST,
            ]
            child = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            output = bytearray()
            overflow = threading.Event()

            def collect():
                while chunk := child.stdout.read(65536):
                    if len(output) + len(chunk) > 14_100_000:
                        overflow.set()
                        return
                    output.extend(chunk)

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            child.stdin.write(job.model_dump_json().encode())
            child.stdin.close()
            deadline = time.monotonic() + 60
            while child.poll() is None:
                if stopped.is_set() or overflow.is_set() or time.monotonic() > deadline:
                    raise RuntimeError("Python execution interrupted")
                stopped.wait(0.1)
            reader.join(timeout=2)
            if child.returncode or overflow.is_set() or reader.is_alive() or stopped.is_set():
                raise RuntimeError("Python execution failed")
            return PythonResponse.model_validate_json(output)
        finally:
            try:
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                    child.stdout.close()
                    child.stdin.close()
                self.slots.release()
