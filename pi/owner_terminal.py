"""Ephemeral Linux owner PTY. Network admission belongs to the owner gateway."""

import os
import signal
import subprocess
import sys
import threading
import time


class TerminalError(RuntimeError):
    pass


class Terminal:
    """No stored commands, credentials, output logs, shell replays or reconnection spawn."""

    def __init__(self, shell, directory, *, lifetime=600, output_limit=1024 * 1024):
        if sys.platform != "linux":
            raise TerminalError("Owner PTY requires Linux.")
        if (
            not os.path.isabs(shell)
            or not os.path.isfile(shell)
            or not os.path.isabs(directory)
            or not os.path.isdir(directory)
            or not 1 <= lifetime <= 900
            or not 4096 <= output_limit <= 1024 * 1024
        ):
            raise TerminalError("Invalid terminal configuration.")
        import pty

        self.lock = threading.RLock()
        self.deadline = time.monotonic() + lifetime
        self.limit, self.buffer, self.offset = output_limit, bytearray(), 0
        self.closed = False
        master, slave = pty.openpty()
        self.master = master
        try:
            os.set_blocking(master, False)
            # Never inherit Pi/gate/provider credentials. Bash startup scripts
            # are disabled; this is still an owner shell, not a filesystem sandbox.
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
                "HOME": directory,
                "HISTFILE": "/dev/null",
            }
            bootstrap = (
                "import fcntl,os,sys,termios; "
                "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
                "os.execv(sys.argv[1],[sys.argv[1],'--noprofile','--norc','-i'])"
            )
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", bootstrap, shell],
                cwd=directory,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.timer = threading.Timer(lifetime, self.close)
        self.timer.daemon = True
        self.timer.start()

    def _active(self):
        if self.closed or time.monotonic() >= self.deadline:
            self.close()
            raise TerminalError("Terminal session expired or closed.")

    def write(self, data):
        if not isinstance(data, bytes) or not 1 <= len(data) <= 8192:
            raise TerminalError("Terminal input must contain 1-8192 bytes.")
        with self.lock:
            self._active()
            try:
                return os.write(self.master, data)
            except (BlockingIOError, OSError):
                raise TerminalError(
                    "Terminal input is unavailable; do not automatically replay."
                ) from None

    def resize(self, rows, columns):
        if (
            type(rows) is not int
            or type(columns) is not int
            or not 2 <= rows <= 200
            or not 10 <= columns <= 400
        ):
            raise TerminalError("Invalid terminal dimensions.")
        import fcntl
        import struct
        import termios

        with self.lock:
            self._active()
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def read(self, cursor=0):
        if type(cursor) is not int or cursor < 0:
            raise TerminalError("Invalid output cursor.")
        with self.lock:
            self._active()
            # Bound work per poll even if a child writes continuously.
            for _ in range(16):
                try:
                    chunk = os.read(self.master, 65536)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                self.buffer.extend(chunk)
            if len(self.buffer) > self.limit:
                excess = len(self.buffer) - self.limit
                del self.buffer[:excess]
                self.offset += excess
            end = self.offset + len(self.buffer)
            if cursor > end:
                raise TerminalError("Output cursor is ahead of this session.")
            return {
                "data": bytes(self.buffer[max(cursor - self.offset, 0) :]),
                "cursor": end,
                "droppedBytes": max(self.offset - cursor, 0),
                "exitCode": self.process.poll(),
            }

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if hasattr(self, "timer"):
                self.timer.cancel()
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(self.master)
                self.process.wait(timeout=5)
                self.buffer.clear()
