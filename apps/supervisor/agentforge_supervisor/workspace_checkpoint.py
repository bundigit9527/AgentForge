"""Durable workspace checkpoints for the single-host Docker desktop pool."""

import os
import selectors
import subprocess
import time

from agentforge_supervisor.desktop import docker

MOUNT_OPTIONS = "size=64m,uid=1000,gid=1000,mode=0700,nosuid,nodev,noexec"
MAX_ARCHIVE = 128 * 1024 * 1024


class CheckpointError(RuntimeError):
    pass


class WorkspaceCheckpoint:
    def __init__(self, directory, manifest, generation, image):
        self.archive = directory / (manifest.stem + ".tar")
        self.partial = self.archive.with_suffix(".partial")
        self.volume = "agentforge-work-" + generation
        self.desktop = "agentforge-desktop-" + generation
        self.helper = "agentforge-checkpoint-" + generation
        self.image = image

    def allocate(self):
        docker(
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=tmpfs",
            "--opt",
            "device=tmpfs",
            "--opt",
            "o=" + MOUNT_OPTIONS,
            self.volume,
        )
        return ("--mount", f"type=volume,src={self.volume},dst=/workspace,volume-nocopy")

    def restore(self):
        if self.archive.exists():
            with self.archive.open("rb") as source:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        self.desktop,
                        "tar",
                        "-C",
                        "/workspace",
                        "--no-same-owner",
                        "-xpf",
                        "-",
                    ],
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    check=True,
                    timeout=60,
                )

    def save(self, *, stopping=False):
        try:
            self._save(stopping)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            raise CheckpointError("Could not save desktop workspace") from exc

    def _save(self, stopping):
        named = docker("volume", "ls", "-q", "--filter", f"name=^{self.volume}$")
        if named != self.volume:
            # A pre-upgrade desktop has an anonymous tmpfs. Read inside the live guest.
            command = [
                "docker",
                "exec",
                self.desktop,
                "tar",
                "-C",
                "/workspace",
                "--sparse",
                "-cf",
                "-",
                ".",
            ]
            self._publish(command)
            return
        already_paused = docker("inspect", "-f", "{{.State.Paused}}", self.desktop) == "true"
        if not already_paused:
            docker("pause", self.desktop)
        success = False
        try:
            self._remove_helper()
            self._publish(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--name",
                    self.helper,
                    "--network=none",
                    "--read-only",
                    "--user",
                    "0:0",
                    "--cap-drop=ALL",
                    "--cap-add=DAC_OVERRIDE",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=16",
                    "--memory=64m",
                    "--log-driver=none",
                    "--no-healthcheck",
                    "--mount",
                    f"type=volume,src={self.volume},dst=/workspace,readonly,volume-nocopy",
                    "--entrypoint",
                    "tar",
                    self.image,
                    "-C",
                    "/workspace",
                    "--sparse",
                    "-cf",
                    "-",
                    ".",
                ]
            )
            success = True
        finally:
            if not success or not stopping:
                docker("unpause", self.desktop)

    def _publish(self, command):
        deadline = time.monotonic() + 60
        try:
            with self.partial.open("wb") as target:
                with subprocess.Popen(command, stdout=subprocess.PIPE) as child:
                    try:
                        with selectors.DefaultSelector() as ready:
                            ready.register(child.stdout, selectors.EVENT_READ)
                            while True:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0 or not ready.select(remaining):
                                    raise TimeoutError("Workspace checkpoint timed out")
                                chunk = os.read(child.stdout.fileno(), 65536)
                                if not chunk:
                                    break
                                if target.tell() + len(chunk) > MAX_ARCHIVE:
                                    raise ValueError("Workspace archive exceeds limit")
                                target.write(chunk)
                        if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                            raise RuntimeError("Could not archive workspace")
                    finally:
                        if child.poll() is None:
                            child.kill()
                target.flush()
                os.fsync(target.fileno())
            self.partial.replace(self.archive)
            folder = os.open(self.archive.parent, os.O_DIRECTORY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        finally:
            self.partial.unlink(missing_ok=True)

    def _remove_helper(self):
        if docker("ps", "-aq", "--filter", f"name=^/{self.helper}$"):
            docker("rm", "-f", self.helper)

    def discard_runtime(self):
        self._remove_helper()
        if docker("volume", "ls", "-q", "--filter", f"name=^{self.volume}$") == self.volume:
            docker("volume", "rm", self.volume)
        self.partial.unlink(missing_ok=True)
