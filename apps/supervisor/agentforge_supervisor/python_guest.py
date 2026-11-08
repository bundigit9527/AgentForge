"""Trusted launcher copied into the isolated container; never run on the host."""

import base64
import json
import os
import resource
import signal
import stat
import subprocess
import sys
from pathlib import Path


def main():
    signal.alarm(55)
    job = json.load(sys.stdin)
    root = Path("/workspace")
    script = root / "_agentforge_job.py"
    script.write_text(job["code"])
    # Limits also apply to descendants. No host paths, credentials or network are present.
    resource.setrlimit(resource.RLIMIT_FSIZE, (11 * 1024 * 1024, 11 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    with open("/tmp/stdout", "w+b") as stdout, open("/tmp/stderr", "w+b") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-I", str(script)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=45)
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()

        def tail(stream):
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 8000))
            return stream.read().decode("utf-8", errors="replace")[-3000:]

        result = dict(exit_code=code, stdout=tail(stdout), stderr=tail(stderr), files=[])
        if code == 124:
            result["stderr"] = "Python execution exceeded 45 seconds."
        if code == 0:
            total = 0
            try:
                for name in job["exports"]:
                    # The parent validates names; repeat checks at the filesystem boundary.
                    if Path(name).name != name or name in (".", ".."):
                        raise ValueError("Invalid export path")
                    fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or not 0 < info.st_size <= 10 * 1024 * 1024
                        ):
                            raise ValueError("Export must be a nonempty regular file <= 10 MiB")
                        total += info.st_size
                        if total > 10 * 1024 * 1024:
                            raise ValueError("Combined exports exceed 10 MiB")
                        data = stream.read(10 * 1024 * 1024 + 1)
                    result["files"].append(dict(name=name, data=base64.b64encode(data).decode()))
            except (OSError, ValueError):
                result.update(
                    exit_code=1, stderr="Requested output missing, unsafe or too large.", files=[]
                )
        print(json.dumps(result))


if __name__ == "__main__":
    main()
