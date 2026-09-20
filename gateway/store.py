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
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "verified_at" not in columns:
                # Existing sessions have no known password-verification time: fail closed.
                db.execute("ALTER TABLE sessions ADD COLUMN verified_at REAL")
            columns = {row[1] for row in db.execute("PRAGMA table_info(login_attempts)")}
            if "attempt_id" not in columns:
                db.execute("ALTER TABLE login_attempts ADD COLUMN attempt_id TEXT")
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

    def configured(self) -> bool:
        with self.transaction() as db:
            return db.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None

    def _prune(self, db: sqlite3.Connection) -> None:
        now = self.clock()
        db.execute(
            "DELETE FROM sessions WHERE expires<=? OR (authenticated=1 AND "
            "(verified_at IS NULL OR verified_at<=?))",
            (now, now - self.idle),
        )
        db.execute(
            "DELETE FROM verification_proofs WHERE expires<=? OR session_id NOT IN "
            "(SELECT id FROM sessions)",
            (now,),
        )
        db.execute("DELETE FROM login_attempts WHERE at<=?", (now - 900,))

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
            "unlock_expires_at": min(expires, now + self.idle) if authenticated else None,
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
        if (
            not row
            or row["expires"] <= now
            or (
                row["authenticated"]
                and (row["verified_at"] is None or row["verified_at"] + self.idle <= now)
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
            min(row["expires"], row["verified_at"] + self.idle) if row["authenticated"] else None
        )
        return value

    def _check_password(self, token: str, password: str, source: str, *, authenticated: bool):
        self.session(token, authenticated=authenticated)
        attempt_id = secrets.token_urlsafe(24)
        with self.transaction() as db:
            self._prune(db)
            self._session(db, token, authenticated=authenticated)
            attempts = db.execute("SELECT count(*) FROM login_attempts").fetchone()[0]
            local = db.execute(
                "SELECT count(*) FROM login_attempts WHERE source=? AND at>?",
                (digest(source), self.clock() - 300),
            ).fetchone()[0]
            if attempts >= 30 or local >= 5:
                raise AuthError("Too many password attempts. Wait fifteen minutes and retry.", 429)
            db.execute(
                "INSERT INTO login_attempts(at,source,attempt_id) VALUES(?,?,?)",
                (self.clock(), digest(source), attempt_id),
            )
            owner = db.execute("SELECT * FROM owner WHERE id=1").fetchone()
        if not owner:
            raise AuthError("No password set. Run conker auth setup on the host.", 409)
        if len(password) > 1024 or not hmac.compare_digest(
            password_hash(password, bytes.fromhex(owner["salt"])), owner["verifier"]
        ):
            raise AuthError(
                "Password not accepted. Retry or use conker auth reset-password on the host."
            )
        return owner["generation"], attempt_id

    def login(self, token: str, password: str, source: str) -> dict:
        generation, attempt_id = self._check_password(token, password, source, authenticated=False)
        with self.transaction() as db:
            current = db.execute("SELECT generation FROM owner WHERE id=1").fetchone()
            self._session(db, token, authenticated=False)
            # A reset or logout during scrypt must not be undone by a late login.
            if not current or current[0] != generation:
                raise AuthError("Credentials changed during login. Start sign-in again.")
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))
            db.execute("DELETE FROM login_attempts WHERE attempt_id=?", (attempt_id,))
            return self._new(db, True, generation)

    def verify(self, token: str, password: str, source: str, fingerprint: str) -> dict:
        generation, attempt_id = self._check_password(token, password, source, authenticated=True)
        with self.transaction() as db:
            # Revalidate after scrypt; a lock/reset/expiry during hashing cannot be undone.
            row = self._session(db, token)
            if row["generation"] != generation:
                raise AuthError("Credentials changed during verification. Sign in again.")
            now = self.clock()
            deadline = min(row["expires"], now + self.idle)
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
                db.execute("DELETE FROM sessions")
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
