"""The Morning check-in's fixed job id, and each run's calendar and mail reads under its chat.

In core because the job (`pi.schedule`), the reads (`pi.context.calendar_events`,
`pi.context.recent_mail`) and forgetting (`pi.actions.tool_forgets`) all need them and cannot
import each other. The reads are made before the run's turn starts (its opening message holds
what they returned), so they are no tool actions of a turn. Each read is instead recorded here,
under the run (whose id becomes its turn's request id) and the chat the turn will run in,
committed before ToolGate is asked (`record`), the way a chat's tool action is recorded before it
is dispatched (`pi.turns.action_identities.prepare`). Forgetting that chat then finds the read
however far the run got, and asks ToolGate to blank its receipt (the events' titles and places,
the mail's senders, subjects and snippets). The table holds ids only, never what was read.
"""

from __future__ import annotations

import hashlib
import secrets
import time

# `pi.schedule.agent_jobs.BUILT_IN` and `pi.schedule.rhythm.MORNING_CHECK_IN` take it from here.
MORNING_CHECK_IN = "job_beca06ba345a8b3170a4d8e2473fe00f"

# Created with `pi.actions.tool_forgets.SCHEMA`, so the forgetting command has it too. One row per
# chat a read's text can reach: a repeated dispatch that opened a new chat adds that chat.
SCHEMA = """
CREATE TABLE IF NOT EXISTS check_in_reads (
 action_id TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL, tool_id TEXT NOT NULL,
 agent_id TEXT, created_at REAL NOT NULL, PRIMARY KEY(action_id, session_id)
);
CREATE INDEX IF NOT EXISTS check_in_reads_chat ON check_in_reads(session_id);
"""


def calendar_action(run_id: str) -> str:
    """The read's action id for one run: a repeated dispatch gets the same receipt back, and
    forgetting the run's chat can name it."""
    return "pi-calendar-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]


def mail_action(run_id: str) -> str:
    """The mail read's action id for one run, made the same way."""
    return "pi-mail-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]


def fresh_action(action_id: str) -> str:
    """A new id, never used before, for the one fresh read made when ToolGate answered
    `action_id` with its stored receipt and that receipt no longer holds the content."""
    return action_id.rsplit("-", 1)[0] + "-" + secrets.token_hex(20)


def record(store, action_id: str, *, run_id: str, session_id: str, tool_id: str, agent_id: str):
    """Before the read is sent: it belongs to this run's chat (committed at once; a write that
    fails raises, and then nothing is read)."""
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO check_in_reads VALUES (?,?,?,?,?,?)",
            (action_id, session_id, run_id, tool_id, agent_id, time.time()),
        )
