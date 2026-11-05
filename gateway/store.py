"""Durable single-owner credentials and revocable, opaque browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

HASH_SLOTS = threading.BoundedSemaphore(1)
DEVICE_SECONDS = 400 * 86400  # Browsers keep a cookie at most 400 days.
DEVICE_ROWS = 20
FAILURE_SECONDS = 30 * 86400
FAILURE_ROWS = 500


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, *, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: bytes) -> str:
    # Bound memory even when several clients attempt expensive password checks.
    with HASH_SLOTS:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024
        ).hex()


class AuthStore:
    """The owner's password, browser sessions and the window a sign-in stays unlocked.

    `idle` is the host's ceiling (GATEWAY_IDLE_TIMEOUT_SECONDS). The owner may pick a shorter
    window in Settings (`idleTimeoutMinutes` in Pi's owner preferences); the gateway keeps the
    last choice it saw in this database (`owner_window`), so a restart or Pi being down never
    loosens it. Until it has seen one, the ceiling applies.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        idle_seconds: int = 1800,
        absolute_seconds: int = 86400,
    ):
        if not 60 <= idle_seconds <= absolute_seconds <= 604800:
            raise ValueError(
                "Session lifetimes must satisfy 60 <= idle <= absolute <= 604800 seconds."
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.idle = idle_seconds
        self.absolute = absolute_seconds
        with self.transaction() as db:
            # executescript commits an existing transaction; explicitly reacquire the
            # write lock so schema creation and migration remain one atomic operation.
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS owner (
                    id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL,
                    verifier TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    csrf TEXT NOT NULL, authenticated INTEGER NOT NULL,
                    generation INTEGER NOT NULL, created REAL NOT NULL,
                    touched REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts (at REAL NOT NULL, source TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_at ON login_attempts(at);
                CREATE TABLE IF NOT EXISTS verification_proofs (
                    token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS owner_window (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    minutes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS known_devices (
                    token_hash TEXT PRIMARY KEY, created REAL NOT NULL, used REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS password_failures (
                    at REAL NOT NULL, origin TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS failures_at ON password_failures(at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "verified_at" not in columns:
                # Existing sessions have no known password-verification time: fail closed.
                db.execute("ALTER TABLE sessions ADD COLUMN verified_at REAL")
            columns = {row[1] for row in db.execute("PRAGMA table_info(login_attempts)")}
            if "attempt_id" not in columns:
                db.execute("ALTER TABLE login_attempts ADD COLUMN attempt_id TEXT")
            if "budget" not in columns:
                # NULL is the shared budget every unknown browser draws from.
                db.execute("ALTER TABLE login_attempts ADD COLUMN budget TEXT")
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def set_password(self, password: str, *, initial: bool = False) -> None:
        if not 15 <= len(password) <= 1024:
            raise AuthError("Use a password or passphrase of 15 to 1024 characters.", 422)
        salt = secrets.token_bytes(16)
        verifier = password_hash(password, salt)
        with self.transaction() as db:
            owner = db.execute("SELECT generation FROM owner WHERE id=1").fetchone()
            if initial and owner:
                raise AuthError("A password already exists. Use reset-password on the host.", 409)
            generation = owner["generation"] + 1 if owner else 1
            db.execute(
                "INSERT OR REPLACE INTO owner VALUES(1,?,?,?)", (salt.hex(), verifier, generation)
            )
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM verification_proofs")
            db.execute("DELETE FROM login_attempts")
            db.execute("DELETE FROM known_devices")

    def _idle(self, db: sqlite3.Connection) -> int:
        """Seconds a sign-in stays unlocked: the owner's choice, never past the host's ceiling."""
        row = db.execute("SELECT minutes FROM owner_window WHERE id=1").fetchone()
        return min(self.idle, row[0] * 60) if row and row[0] else self.idle

    def owner_window(self, revision: int, minutes: int, *, saved: bool) -> bool:
        """Record the owner's sign-out window from a preferences document Pi returned.

        `minutes` 0 means no window shorter than the host's. A save is his latest choice and always
        wins. A read wins when its revision is not older than the one kept; an older read (one that
        raced a save) may only shorten the window, so a late answer never loosens it. Returns
        whether the value was kept.
        """
        if (
            type(revision) is not int
            or revision < 1
            or type(minutes) is not int
            or not 0 <= minutes <= 10080
        ):
            return False
        with self.transaction() as db:
            row = db.execute("SELECT revision,minutes FROM owner_window WHERE id=1").fetchone()
            if not saved and row and revision < row["revision"]:
                kept = row["minutes"] or float("inf")
                if (minutes or float("inf")) >= kept:
                    return False
            db.execute("INSERT OR REPLACE INTO owner_window VALUES(1,?,?)", (revision, minutes))
            return True

    def configured(self) -> bool:
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None

    def _prune(self, db: sqlite3.Connection) -> None:
        now = self.clock()
        db.execute(
            "DELETE FROM sessions WHERE expires<=? OR (authenticated=1 AND "
            "(verified_at IS NULL OR verified_at<=?))",
            (now, now - self._idle(db)),
        )
        db.execute(
            "DELETE FROM verification_proofs WHERE expires<=? OR session_id NOT IN "
            "(SELECT id FROM sessions)",
            (now,),
        )
        db.execute("DELETE FROM login_attempts WHERE at<=?", (now - 900,))
        db.execute("DELETE FROM known_devices WHERE used<=?", (now - DEVICE_SECONDS,))
        db.execute("DELETE FROM password_failures WHERE at<=?", (now - FAILURE_SECONDS,))
        db.execute(
            "DELETE FROM password_failures WHERE rowid NOT IN "
            "(SELECT rowid FROM password_failures ORDER BY at DESC LIMIT ?)",
            (FAILURE_ROWS,),
        )

    def _new(self, db: sqlite3.Connection, authenticated: bool, generation: int) -> dict:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.clock()
        identity = secrets.token_urlsafe(18)
        expires = now + (self.absolute if authenticated else 600)
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                digest(token),
                identity,
                csrf,
                int(authenticated),
                generation,
                now,
                now,
                expires,
                now if authenticated else None,
            ),
        )
        return {
            "token": token,
            "id": identity,
            "csrf": csrf,
            "authenticated": authenticated,
            "expires": expires,
            "unlock_expires_at": min(expires, now + self._idle(db)) if authenticated else None,
        }

    def anonymous(self) -> dict:
        with self.transaction() as db:
            self._prune(db)
            count = db.execute("SELECT count(*) FROM sessions WHERE authenticated=0").fetchone()[0]
            if count >= 1000:
                raise AuthError("Too many login sessions. Wait ten minutes and retry.", 429)
            return self._new(db, False, 0)

    def session(self, token: str, *, authenticated: bool = True) -> dict:
        with self.transaction() as db:
            row = self._session(db, token, authenticated=authenticated)
            db.execute(
                "UPDATE sessions SET touched=? WHERE token_hash=?", (self.clock(), digest(token))
            )
            return row

    def _session(self, db: sqlite3.Connection, token: str, *, authenticated: bool = True) -> dict:
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (digest(token),)).fetchone()
        now = self.clock()
        idle = self._idle(db)
        if (
            not row
            or row["expires"] <= now
            or (
                row["authenticated"]
                and (row["verified_at"] is None or row["verified_at"] + idle <= now)
            )
        ):
            raise AuthError("Session expired or revoked. Sign in again.")
        if authenticated and not row["authenticated"]:
            raise AuthError("Sign in to continue.")
        owner = db.execute("SELECT generation FROM owner WHERE id=1").fetchone()
        if row["authenticated"] and (not owner or owner[0] != row["generation"]):
            raise AuthError("Password changed. Sign in again.")
        value = dict(row)
        value["unlock_expires_at"] = (
            min(row["expires"], row["verified_at"] + idle) if row["authenticated"] else None
        )
        return value

    def _budget(self, db: sqlite3.Connection, row: dict, device: str) -> str | None:
        """Whose attempts a password check counts against; None is the shared budget.

        A signed-in session confirming an action has its own budget: it already proved the
        password once, so strangers filling the shared budget cannot stop him approving. A
        browser that signed in before (a known-device cookie, kept here only hashed) has its
        own too, so a flood of wrong passwords from elsewhere cannot lock him out of signing in.
        """
        if row["authenticated"]:
            return "session:" + row["id"]
        if device and len(device) <= 128:
            hashed = digest(device)
            if db.execute("SELECT 1 FROM known_devices WHERE token_hash=?", (hashed,)).fetchone():
                return "device:" + hashed
        return None

    def _check_password(
        self, token: str, password: str, source: str, *, authenticated: bool, device: str = ""
    ):
        self.session(token, authenticated=authenticated)
        attempt_id = secrets.token_urlsafe(24)
        with self.transaction() as db:
            self._prune(db)
            row = self._session(db, token, authenticated=authenticated)
            budget = self._budget(db, row, device)
            since = self.clock() - 300
            if budget is not None:
                own = db.execute(
                    "SELECT count(*) FROM login_attempts WHERE budget=? AND at>?", (budget, since)
                ).fetchone()[0]
                blocked = own >= 5
            else:
                attempts = db.execute(
                    "SELECT count(*) FROM login_attempts WHERE budget IS NULL"
                ).fetchone()[0]
                local = db.execute(
                    "SELECT count(*) FROM login_attempts WHERE budget IS NULL AND source=? AND at>?",
                    (digest(source), since),
                ).fetchone()[0]
                blocked = attempts >= 30 or local >= 5
            if blocked:
                raise AuthError("Too many password attempts. Wait fifteen minutes and retry.", 429)
            db.execute(
                "INSERT INTO login_attempts(at,source,attempt_id,budget) VALUES(?,?,?,?)",
                (self.clock(), digest(source), attempt_id, budget),
            )
            owner = db.execute("SELECT * FROM owner WHERE id=1").fetchone()
        if not owner:
            raise AuthError("No password set. Run conker auth setup on the host.", 409)
        if len(password) > 1024 or not hmac.compare_digest(
            password_hash(password, bytes.fromhex(owner["salt"])), owner["verifier"]
        ):
            origin = "signed_in" if authenticated else "known_device" if budget else "new_device"
            with self.transaction() as db:
                db.execute(
                    "INSERT INTO password_failures(at,origin) VALUES(?,?)", (self.clock(), origin)
                )
            raise AuthError(
                "Password not accepted. Retry or use conker auth reset-password on the host."
            )
        return owner["generation"], attempt_id

    def login(self, token: str, password: str, source: str, device: str = "") -> dict:
        """Sign in. The result's `device` is the known-device token the browser keeps."""
        generation, attempt_id = self._check_password(
            token, password, source, authenticated=False, device=device
        )
        with self.transaction() as db:
            current = db.execute("SELECT generation FROM owner WHERE id=1").fetchone()
            self._session(db, token, authenticated=False)
            # A reset or logout during scrypt must not be undone by a late login.
            if not current or current[0] != generation:
                raise AuthError("Credentials changed during login. Start sign-in again.")
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))
            db.execute("DELETE FROM login_attempts WHERE attempt_id=?", (attempt_id,))
            # A fresh token every sign-in; the one presented (if any) stops counting.
            if device and len(device) <= 128:
                db.execute("DELETE FROM known_devices WHERE token_hash=?", (digest(device),))
            kept, now = secrets.token_urlsafe(32), self.clock()
            db.execute("INSERT INTO known_devices VALUES(?,?,?)", (digest(kept), now, now))
            db.execute(
                "DELETE FROM known_devices WHERE token_hash NOT IN "
                "(SELECT token_hash FROM known_devices ORDER BY used DESC LIMIT ?)",
                (DEVICE_ROWS,),
            )
            return {**self._new(db, True, generation), "device": kept}

    def verify(self, token: str, password: str, source: str, fingerprint: str) -> dict:
        generation, attempt_id = self._check_password(token, password, source, authenticated=True)
        with self.transaction() as db:
            # Revalidate after scrypt; a lock/reset/expiry during hashing cannot be undone.
            row = self._session(db, token)
            if row["generation"] != generation:
                raise AuthError("Credentials changed during verification. Sign in again.")
            now = self.clock()
            deadline = min(row["expires"], now + self._idle(db))
            proof, expires = secrets.token_urlsafe(32), min(now + 120, deadline)
            db.execute(
                "UPDATE sessions SET verified_at=?,touched=? WHERE id=?", (now, now, row["id"])
            )
            db.execute("DELETE FROM login_attempts WHERE attempt_id=?", (attempt_id,))
            db.execute(
                "INSERT INTO verification_proofs VALUES(?,?,?,?,?)",
                (digest(proof), row["id"], generation, fingerprint, expires),
            )
            return {
                "verification_token": proof,
                "verification_expires_at": expires,
                "unlock_expires_at": deadline,
            }

    def consume(self, token: str, proof: str, fingerprint: str) -> None:
        with self.transaction() as db:
            row = self._session(db, token)
            found = (
                db.execute(
                    "SELECT * FROM verification_proofs WHERE token_hash=?", (digest(proof),)
                ).fetchone()
                if len(proof) <= 128
                else None
            )
            if (
                not found
                or found["session_id"] != row["id"]
                or found["generation"] != row["generation"]
                or found["expires"] <= self.clock()
                or not hmac.compare_digest(found["fingerprint"], fingerprint)
            ):
                raise AuthError(
                    "Verify your password for this exact operation before submitting.",
                    428,
                    code="verification_required",
                )
            # Commit consumption before dispatch. Never restore authority on an uncertain response.
            db.execute("DELETE FROM verification_proofs WHERE token_hash=?", (digest(proof),))

    def revoke(self, session_id: str | None = None) -> None:
        with self.transaction() as db:
            if session_id is None:
                # Signing out everywhere also forgets every browser that signed in before.
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM known_devices")
            else:
                db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            db.execute(
                "DELETE FROM verification_proofs WHERE session_id NOT IN (SELECT id FROM sessions)"
            )

    def sessions(self) -> list[dict]:
        with self.transaction() as db:
            self._prune(db)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,created,touched,expires FROM sessions "
                    "WHERE authenticated=1 ORDER BY created"
                )
            ]

    def alerts(self) -> dict:
        """Wrong passwords of the last 30 days, newest first, and how many he has not seen.

        `origin` says where: `new_device` (a browser that never signed in here),
        `known_device` (one that did) or `signed_in` (confirming an action in a signed-in tab).
        """
        with self.transaction() as db:
            self._prune(db)
            rows = db.execute(
                "SELECT at,origin,seen FROM password_failures ORDER BY at DESC LIMIT 50"
            ).fetchall()
            unseen = db.execute("SELECT count(*) FROM password_failures WHERE seen=0").fetchone()
        return {
            "unseen": unseen[0],
            "results": [
                {"at": row["at"], "origin": row["origin"], "seen": bool(row["seen"])}
                for row in rows
            ],
        }

    def alerts_seen(self) -> None:
        """He saw them ("That was me"); a wrong password after this alerts again."""
        with self.transaction() as db:
            db.execute(
                "UPDATE password_failures SET seen=1 WHERE seen=0 AND at<=?", (self.clock(),)
            )
