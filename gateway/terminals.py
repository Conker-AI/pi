"""Ephemeral owner-session terminal leases; no command or token persistence."""

import re
import threading

from pi.owner_terminal import Terminal, TerminalError


class Terminals:
    def __init__(self, shell, directory, *, factory=Terminal, interval=1):
        self.shell, self.directory, self.factory = shell, directory, factory
        self.lock = threading.RLock()
        self.entries = {}
        self.stop = threading.Event()
        self.interval = interval
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def create(self, owner, identity, validate, authorize):
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", identity):
            raise TerminalError("Invalid terminal request identity.")
        with self.lock:
            if self.stop.is_set():
                raise TerminalError("Terminal service is closed.")
            previous = self.entries.get(identity)
            if previous:
                if previous["owner"] != owner:
                    raise TerminalError("Terminal belongs to another browser session.")
                validate()
                return {"id": identity, "closed": previous["terminal"].closed, "replayed": True}
            if (
                len(self.entries) >= 1000
                or sum(not entry["terminal"].closed for entry in self.entries.values()) >= 4
            ):
                raise TerminalError("Terminal session limit reached.")
            if any(
                entry["owner"] == owner and not entry["terminal"].closed
                for entry in self.entries.values()
            ):
                raise TerminalError("Close this browser session's existing terminal first.")
            validate()
            authorize()
            try:
                terminal = self.factory(self.shell, self.directory)
            except (OSError, ValueError):
                raise TerminalError(
                    "Terminal could not start. Check operator configuration."
                ) from None
            self.entries[identity] = {"owner": owner, "terminal": terminal, "validate": validate}
            return {"id": identity, "closed": False, "replayed": False}

    def use(self, owner, identity, operation, *args):
        with self.lock:
            entry = self.entries.get(identity)
            if entry is None or entry["owner"] != owner:
                raise TerminalError("Terminal is unavailable for this browser session.")
            try:
                entry["validate"]()
            except Exception:
                entry["terminal"].close()
                raise TerminalError("Terminal authorization expired.") from None
            if operation not in ("read", "write", "resize", "close"):
                raise TerminalError("Invalid terminal operation.")
            return getattr(entry["terminal"], operation)(*args)

    def sweep(self):
        with self.lock:
            for entry in self.entries.values():
                if entry["terminal"].closed:
                    continue
                try:
                    entry["validate"]()
                except Exception:
                    entry["terminal"].close()

    def _watch(self):
        while not self.stop.wait(self.interval):
            self.sweep()

    def close(self):
        self.stop.set()
        with self.lock:
            for entry in self.entries.values():
                entry["terminal"].close()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=6)
