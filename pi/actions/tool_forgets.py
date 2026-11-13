"""Ask ToolGate to forget a forgotten chat's tool records (forgetting gap 9).

ToolGate keeps, per approval request and per action, the tool's arguments and results. Forgetting
a chat runs with Pi stopped, so it only queues here (`queue`) the ids of the chat's actions
(`tool_actions`) and of every approval request its turns made, answered ones included. Once Pi
runs again, `drain` sends them to ToolGate's `POST /v2/agent/forget` as the agent that made them
(`ToolGateClient.forget_records`), which blanks their arguments and results and keeps the audit
line. Every request a turn parked on is kept as it parks (`remember`, `turn_approval_requests`),
because the turn itself holds only the latest. A request still waiting for the owner is refused until its withdrawal
(`pi.actions.withdrawals`) lands, so it is simply tried again later, as is one still running.
`unknown` (another key's, one that never reached ToolGate, or a ToolGate restored without it) is
gone. Pi's key being refused is held as `blocked`, reported in Pi's health and still retried. A
failure never undoes the forgetting. The queue holds ids, never text.
"""

from __future__ import annotations

import re
import time

from pi.actions.toolgate import ToolGateUnavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS toolgate_forgets (
 kind TEXT NOT NULL CHECK(kind IN ('request','action')), id TEXT NOT NULL, agent_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('pending','forgotten','gone','blocked')),
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY(kind, id)
);
CREATE INDEX IF NOT EXISTS toolgate_forgets_due ON toolgate_forgets(state, next_at);
CREATE TABLE IF NOT EXISTS turn_approval_requests (
 turn_id TEXT NOT NULL, request_id TEXT NOT NULL, PRIMARY KEY(turn_id, request_id)
);
"""
# ToolGate's own limits for one call: ids it accepts, and how many of each kind.
RECORD_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,160}")
BATCH = 200
# Pi's key itself was refused: retrying cannot help until the owner repairs it.
BLOCKED = {"forget_401", "forget_403"}
KINDS = {"request": "requests", "action": "actions"}


def initialize(db):
    db.executescript(SCHEMA)


def remember(db, turn_id, request_id):
    """Each request a turn parks on, kept: `turns.approval_request_id` holds only the latest, and a
    turn that parks again (a second tool, or an expired approval asked again) overwrites it."""
    if request_id:
        db.execute(
            "INSERT OR IGNORE INTO turn_approval_requests(turn_id, request_id) VALUES (?,?)",
            (turn_id, request_id),
        )


def records(db, session_ids) -> list[tuple[str, str, str]]:
    """(kind, id, agent) of every action and approval request these chats' turns made."""
    if not session_ids:
        return []
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "turns" not in tables:
        return []
    columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
    agent = (
        "CASE WHEN json_valid(s.snapshot) THEN json_extract(s.snapshot,'$.agentId') END"
        if "turn_settings" in tables
        else "NULL"
    )
    join = "LEFT JOIN turn_settings s ON s.turn_id=t.id " if "turn_settings" in tables else ""
    marks = ",".join("?" for _ in session_ids)
    found = []
    requests = []
    if "approval_request_id" in columns:
        requests.append(
            "SELECT t.id AS turn_id, t.approval_request_id AS request_id FROM turns t "
            "WHERE t.approval_request_id IS NOT NULL"
        )
    if "turn_approval_requests" in tables:
        requests.append("SELECT turn_id, request_id FROM turn_approval_requests")
    if requests:
        found += [
            ("request", row[0], row[1])
            for row in db.execute(
                f"SELECT DISTINCT r.request_id, {agent} FROM ({' UNION '.join(requests)}) r "
                f"JOIN turns t ON t.id=r.turn_id {join}"
                f"WHERE t.session_id IN ({marks}) ORDER BY r.request_id",
                session_ids,
            )
        ]
    if "tool_actions" in tables:
        found += [
            ("action", row[0], row[1])
            for row in db.execute(
                f"SELECT a.id, {agent} FROM tool_actions a JOIN turns t ON t.id=a.turn_id {join}"
                f"WHERE t.session_id IN ({marks}) ORDER BY a.id",
                session_ids,
            )
        ]
    return [(kind, identity, owner or "companion") for kind, identity, owner in found]


def queue(db, session_ids, now=None) -> int:
    """Inside the forgetting transaction: queue each record once."""
    found = records(db, session_ids)
    when = time.time() if now is None else now
    db.executemany(
        "INSERT OR IGNORE INTO toolgate_forgets(kind,id,agent_id,state,created_at) "
        "VALUES (?,?,?,'pending',?)",
        [(kind, identity, agent, when) for kind, identity, agent in found],
    )
    return len(found)


def _settle(store, rows, outcome, now):
    """Write each row's new state: `outcome` maps (kind, id) to (state, error)."""
    with store._connect() as db:
        for row in rows:
            state, error = outcome[(row["kind"], row["id"])]
            wait = min(3600, 30 * 2 ** min(row["attempts"], 7))
            db.execute(
                "UPDATE toolgate_forgets SET state=?, attempts=attempts+1, next_at=?, error=? "
                "WHERE kind=? AND id=?",
                (state, now + wait, error, row["kind"], row["id"]),
            )


def _send(client, rows, agent) -> dict:
    """One call for these rows as `agent`; (kind, id) -> (state, error), with `unknown` ids
    sent once more as the key itself (requests made before agent identities carry its id)."""
    ids = {kind: [row["id"] for row in rows if row["kind"] == kind] for kind in KINDS}
    outcome = {}
    for acting in dict.fromkeys([agent, None]):
        if not ids["request"] and not ids["action"]:
            break
        try:
            answer = client.forget_records(ids["request"], ids["action"], acting)
        except ToolGateUnavailable as exc:
            # The agent itself refused (a name ToolGate no longer takes): ask as the key.
            if acting is None or exc.reason not in BLOCKED:
                raise
            continue
        unknown = {kind: [] for kind in KINDS}
        for kind, field in KINDS.items():
            part = answer[field]
            for identity in (*part["forgotten"], *part["already"]):
                outcome[(kind, identity)] = ("forgotten", "")
            for refusal in part["refused"]:
                outcome[(kind, refusal["id"])] = (
                    "pending",
                    "ToolGate is still busy with it; trying again later.",
                )
            unknown[kind] = [identity for identity in part["unknown"] if identity in ids[kind]]
        ids = unknown
    for kind, rest in ids.items():
        for identity in rest:
            outcome[(kind, identity)] = ("gone", "")
    return outcome


def drain(store, client, now=None, limit=BATCH) -> int:
    """Send up to `limit` queued records of each kind; how many are settled now."""
    if client is None or not getattr(client, "execution_key", "x"):
        return 0
    now = time.time() if now is None else now
    with store._connect() as db:
        rows = [
            row
            for kind in KINDS
            for row in db.execute(
                "SELECT * FROM toolgate_forgets WHERE kind=? AND state IN ('pending','blocked') "
                "AND next_at<=? ORDER BY next_at, id LIMIT ?",
                (kind, now, limit),
            ).fetchall()
        ]
    settled = 0
    for agent in dict.fromkeys(row["agent_id"] for row in rows):
        group = [row for row in rows if row["agent_id"] == agent]
        # An id ToolGate would refuse the whole call for never reached it: nothing to forget.
        outcome = {(row["kind"], row["id"]): ("gone", "") for row in group}
        sendable = [row for row in group if RECORD_ID.fullmatch(row["id"] or "")]
        try:
            sent = _send(client, sendable, agent)
        except ToolGateUnavailable as exc:
            state = "blocked" if exc.reason in BLOCKED else "pending"
            error = (
                "ToolGate refused Pi's key; Pi tries again once it is repaired."
                if state == "blocked"
                else "ToolGate did not answer; trying again later."
            )
            sent = {(row["kind"], row["id"]): (state, error) for row in sendable}
        for row in sendable:
            # An id missing from ToolGate's answer is asked again, never assumed forgotten.
            outcome[(row["kind"], row["id"])] = sent.get(
                (row["kind"], row["id"]), ("pending", "Not answered yet; trying again later.")
            )
        _settle(store, group, outcome, now)
        settled += sum(
            outcome[(row["kind"], row["id"])][0] in ("forgotten", "gone") for row in group
        )
    return settled
