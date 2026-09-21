"""Stdlib Linux PTY check; temporary workspace and synthetic commands only."""

import os
import tempfile
import time

from pi.owner_terminal import Terminal, TerminalError


def check():
    os.environ["PI_SYNTHETIC_SECRET"] = "must-not-be-inherited"
    with tempfile.TemporaryDirectory() as directory:
        terminal = Terminal("/bin/bash", directory, lifetime=5, output_limit=4096)
        try:
            terminal.resize(24, 80)
            command = (
                b"printf 'PTY_OK'; printf '%s' \"${PI_SYNTHETIC_SECRET-unset}\"; printf '\\n'\n"
            )
            assert terminal.write(command) == len(command)
            output = b""
            deadline = time.monotonic() + 3
            cursor = 0
            while time.monotonic() < deadline:
                value = terminal.read(cursor)
                output += value["data"]
                cursor = value["cursor"]
                if b"PTY_OKunset" in output:
                    break
                time.sleep(0.02)
            assert b"PTY_OKunset" in output, repr(output)
            assert b"must-not-be-inherited" not in output
            assert b"no job control" not in output
            assert terminal.read(cursor)["cursor"] >= cursor
        finally:
            terminal.close()
        terminal.close()
        try:
            terminal.write(b"echo no\n")
        except TerminalError:
            pass
        else:
            raise AssertionError("Closed terminal accepted input")
        expiring = Terminal("/bin/bash", directory, lifetime=1)
        time.sleep(1.2)
        assert expiring.closed and expiring.process.poll() is not None
    print(
        "Linux PTY: input/output, clean environment, job control, close and expiry passed"
    )


if __name__ == "__main__":
    check()
