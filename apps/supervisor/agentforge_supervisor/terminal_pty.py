"""Runs inside the disposable Linux container via docker exec -i."""

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
from pathlib import Path
from uuid import UUID

MAX_INPUT = 65536


def frames(buffer):
    while len(buffer) >= 5:
        kind, length = struct.unpack_from(">BI", buffer)
        if kind not in (0, 1) or length > MAX_INPUT or (kind == 1 and length != 4):
            raise ValueError("Invalid terminal frame")
        if len(buffer) < 5 + length:
            return
        payload = bytes(buffer[5 : 5 + length])
        del buffer[: 5 + length]
        yield kind, payload


def main(bot):
    root = os.open("/workspace", os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if bot:
            folder = str(UUID(bot))
            try:
                os.mkdir(folder, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            bot_dir = os.open(folder, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                os.fchdir(bot_dir)
            finally:
                os.close(bot_dir)
        else:
            os.fchdir(root)
    finally:
        os.close(root)
    child, master = pty.fork()
    if child == 0:
        os.environ.update(HOME="/tmp/home", DISPLAY=":1", TERM="xterm-256color")
        os.execl("/bin/bash", "bash", "--noprofile", "--norc", "-i")
    ended = False
    pending, inputs, output = bytearray(), bytearray(), bytearray()
    os.set_blocking(master, False)
    os.set_blocking(1, False)
    try:
        while True:
            if ended and not output:
                break
            reads = [0, master] if not ended and len(output) < 65536 else [0]
            writes = ([master] if inputs else []) + ([1] if output else [])
            readable, writable, _ = select.select(reads, writes, [])
            if 0 in readable:
                data = os.read(0, 65536)
                if not data:
                    break
                pending.extend(data)
                for kind, payload in frames(pending):
                    if kind == 0:
                        inputs.extend(payload)
                        if len(inputs) > 256 * 1024:
                            raise ValueError("Terminal input backlog exceeded")
                    else:
                        cols, rows = struct.unpack(">HH", payload)
                        if not (1 <= cols <= 1000 and 1 <= rows <= 1000):
                            raise ValueError("Invalid terminal size")
                        fcntl.ioctl(
                            master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
                        )
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                ended = not data
                output.extend(data)
            if master in writable:
                del inputs[: os.write(master, inputs)]
            if 1 in writable:
                del output[: os.write(1, output)]
    finally:
        # Kill the interactive shell and its current foreground job before relinquishing the PTY.
        try:
            fg = os.tcgetpgrp(master)
            if fg > 0:
                os.killpg(fg, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Also terminate background jobs that still belong to this login session.
        for info in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = info.read_bytes().rsplit(b")", 1)[1].split()
                if int(fields[3]) == child:
                    os.kill(int(info.parent.name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
        os.close(master)
        os.waitpid(child, 0)


if __name__ == "__main__":
    main(sys.argv[1])
