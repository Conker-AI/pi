import pytest

from gateway.terminals import Terminals
from pi.owner_terminal import TerminalError


class FakeTerminal:
    def __init__(self, *args):
        self.closed = False
        self.writes = []

    def close(self):
        self.closed = True

    def write(self, value):
        self.writes.append(value)
        return len(value)


def test_replay_is_one_spawn_and_authorization_is_required():
    spawned, approved = [], []

    def factory(*args):
        terminal = FakeTerminal()
        spawned.append(terminal)
        return terminal

    manager = Terminals("/bin/bash", "/synthetic", factory=factory)
    try:
        def create():
            return manager.create(
                "owner", "terminal_request_01", lambda: None, lambda: approved.append(True)
            )
        assert not create()["replayed"]
        assert create()["replayed"]
        assert len(spawned) == len(approved) == 1
        with pytest.raises(TerminalError):
            manager.use("other", "terminal_request_01", "write", b"not allowed")
        assert manager.use("owner", "terminal_request_01", "write", b"input") == 5
        manager.use("owner", "terminal_request_01", "close")
        assert create()["closed"] and len(spawned) == 1
    finally:
        manager.close()


def test_revocation_closes_without_client_polling_and_denied_create_does_not_spawn():
    valid = [True]

    def validate():
        if not valid[0]:
            raise PermissionError()

    manager = Terminals("/bin/bash", "/synthetic", factory=FakeTerminal)
    try:
        manager.create("owner", "terminal_request_01", validate, lambda: None)
        terminal = manager.entries["terminal_request_01"]["terminal"]
        valid[0] = False
        manager.sweep()
        assert terminal.closed
        with pytest.raises(PermissionError):
            manager.create("owner", "terminal_request_02", validate, lambda: pytest.fail())
        assert "terminal_request_02" not in manager.entries
    finally:
        manager.close()
