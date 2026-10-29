"""Every package's tables, triggers and migrations, installed in dependency order."""

from __future__ import annotations

from pi.actions import identities as actions
from pi.actions import withdrawals
from pi.agents import (
    asks,
    change_requests,
    characters,
    continuity,
    keep,
    skills,
    team_execution,
    tool_drafts,
    workflow_offers,
    workflow_proposals,
)
from pi.agents import profiles as agents
from pi.agents import teams as collaboration
from pi.calls import runtime as calls
from pi.content import artifacts, attachments
from pi.context import compaction as context_compaction
from pi.context import controls as context_controls
from pi.context import decisions, project_context, projects, research_usage, universal_search
from pi.context import retrieval as context_retrieval
from pi.context import summaries as context_summaries
from pi.conversation import citations as message_citations
from pi.conversation import drafts, fact_checks, judgements, naming, output_settings, submissions
from pi.conversation import forks as message_forks
from pi.conversation import observation as session_observation
from pi.conversation import retries as response_retries
from pi.conversation import settings as session_settings
from pi.conversation import versions as response_versions
from pi.host import actions as system_actions
from pi.host import filesystem_reads
from pi.host import inventory as system_inventory
from pi.memory import edits as memory_edits
from pi.memory import proposals as memory_proposals
from pi.memory import store as memory_store
from pi.models import evaluations as model_evaluations
from pi.models import roles as model_roles
from pi.models import service_inference
from pi.owner import plan_alerts
from pi.owner import preferences as owner_preferences
from pi.owner import settings as owner_settings
from pi.schedule import (
    agent_hooks,
    agent_jobs,
    agent_triggers,
    jobs,
    proactive_budget,
    proposals,
    tasks,
)
from pi.setup import choices as setup_choices
from pi.setup import model_probes as setup_model_probes
from pi.setup import protection as setup_protection
from pi.setup import receipts as setup_receipts
from pi.setup import rehearsal as setup_rehearsal
from pi.turns import access as turn_access
from pi.turns import context as turn_context
from pi.turns import control as turn_control
from pi.turns import queue as turn_queue
from pi.turns import restart as turn_restart
from pi.turns import steering as turn_steering
from pi.turns import thinking as background_thinking


def install(db):
    """Install all package schemas, triggers, and run migrations in dependency order."""
    db.executescript(session_settings.SCHEMA)
    db.executescript(skills.SCHEMA)
    setup_receipts.initialize(db)
    setup_choices.initialize(db)
    setup_model_probes.initialize(db)
    setup_protection.initialize(db)
    setup_rehearsal.initialize(db)
    db.executescript(memory_store.SCHEMA)
    db.executescript(memory_edits.SCHEMA)
    db.executescript(memory_proposals.SCHEMA)
    db.executescript(proposals.SCHEMA)
    memory_store.migrate(db)
    db.executescript(actions.SCHEMA)
    db.executescript(tasks.SCHEMA)
    db.executescript(submissions.SCHEMA)
    db.executescript(turn_control.SCHEMA)
    db.executescript(turn_queue.SCHEMA)
    db.executescript(message_forks.SCHEMA)
    db.executescript(turn_context.SCHEMA)
    db.executescript(response_versions.SCHEMA)
    db.executescript(response_retries.SCHEMA)
    db.executescript(judgements.SCHEMA)
    db.executescript(fact_checks.SCHEMA)
    db.executescript(turn_steering.SCHEMA)
    db.executescript(turn_restart.SCHEMA)
    db.executescript(research_usage.SCHEMA)
    research_usage.migrate(db)
    db.executescript(universal_search.SCHEMA)
    db.executescript(message_citations.SCHEMA)
    owner_preferences.initialize(db)
    owner_settings.initialize(db)
    db.executescript(proactive_budget.SCHEMA)
    plan_alerts.initialize(db)
    agent_jobs.initialize(db)
    agent_triggers.initialize(db)
    agent_hooks.initialize(db)
    workflow_proposals.initialize(db)
    change_requests.initialize(db)
    keep.initialize(db)
    withdrawals.initialize(db)
    tool_drafts.initialize(db)
    asks.initialize(db)
    turn_access.initialize(db)
    workflow_offers.initialize(db)
    agents.initialize(db)
    db.executescript(projects.SCHEMA)
    db.executescript(project_context.SCHEMA)
    db.executescript(context_controls.SCHEMA)
    db.executescript(context_compaction.SCHEMA)
    db.executescript(context_retrieval.SCHEMA)
    db.executescript(collaboration.SCHEMA)
    collaboration.initialize(db)
    db.executescript(team_execution.SCHEMA)
    db.executescript(artifacts.SCHEMA)
    db.executescript(model_roles.SCHEMA)
    db.executescript(service_inference.SCHEMA)
    db.executescript(background_thinking.SCHEMA)
    db.executescript(jobs.SCHEMA)
    continuity.initialize(db)
    db.executescript(drafts.SCHEMA)
    db.executescript(context_summaries.SCHEMA)
    attachments.initialize(db)
    db.executescript(model_evaluations.SCHEMA)
    db.executescript(calls.SCHEMA)
    db.executescript(characters.SCHEMA)
    db.executescript(system_inventory.SCHEMA)
    system_inventory.initialize(db)
    db.executescript(filesystem_reads.SCHEMA)
    db.executescript(system_actions.SCHEMA)
    session_observation.initialize(db)
    db.executescript(output_settings.SCHEMA)
    db.executescript(naming.SCHEMA)
    db.executescript(decisions.SCHEMA)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT REFERENCES sessions(id),
    title        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',   -- open | forked | closed
    created_at   REAL NOT NULL,
    closed_at    REAL,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    seq          INTEGER NOT NULL,
    role         TEXT NOT NULL,                  -- user | assistant | system | tool
    content      TEXT NOT NULL,                  -- JSON
    created_at   REAL NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS turns (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    status       TEXT NOT NULL,                  -- running | complete | interrupted | failed
                                                 --  | awaiting_approval | acted_no_reply
    provider     TEXT,
    model        TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
    route_tier   TEXT,
    route_reason TEXT,
    -- The parked action, kept whole so the retry is the same action the owner
    -- was shown. Reconstructing it later from anything else would be a
    -- different action wearing the same approval.
    approval_request_id TEXT,
    approval_tool_id    TEXT,
    approval_args       TEXT,
    approval_expires_at TEXT,
    -- The owner's own words that started this turn, kept beside the action they
    -- led to. An approval that shows only what Conker wants to do cannot be
    -- judged: the question is whether it follows from what was actually asked.
    -- Anything ingested since is untrusted, so the *owner's* text is the only
    -- honest anchor to compare against.
    approval_intent     TEXT,
    -- Whether this turn changed the world. A different fact from whether it
    -- produced a reply, and the one the owner most needs to be true: a tool
    -- that ran and a model that then failed to narrate it is not a turn where
    -- nothing happened. Written before the narration is attempted, so a crash
    -- in between cannot lose it.
    acted        INTEGER NOT NULL DEFAULT 0,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS sessions_by_created_at ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS sessions_history_order ON sessions(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, started_at);

-- Append-only, enforced by the database rather than by convention. A caller
-- that reaches past this module still cannot rewrite what was said.
CREATE TRIGGER IF NOT EXISTS messages_are_immutable
BEFORE UPDATE ON messages
BEGIN
    SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER IF NOT EXISTS messages_are_permanent
BEFORE DELETE ON messages
BEGIN
    SELECT RAISE(ABORT, 'messages are append-only');
END;
"""


# A receipt contains identifiers and time, never text or a hash of deleted text.
FORGETTING_SCHEMA = """
CREATE TABLE IF NOT EXISTS forgetting_receipts (
    id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL REFERENCES sessions(id),
    forgotten_at REAL NOT NULL,
    confirmation TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    turn_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forgotten_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    receipt_id TEXT NOT NULL REFERENCES forgetting_receipts(id)
);
CREATE TABLE IF NOT EXISTS forgetting_maintenance (
    id INTEGER PRIMARY KEY CHECK (id=1),
    root_session_id TEXT NOT NULL,
    confirmation TEXT NOT NULL
);
"""

for _table in ("forgetting_receipts", "forgotten_sessions"):
    _key = "id" if _table == "forgetting_receipts" else "session_id"
    # SQLite REPLACE can delete without firing DELETE triggers when recursive
    # triggers are off. Reject duplicate identities before that path is taken.
    FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_replace
BEFORE INSERT ON {_table}
WHEN EXISTS (SELECT 1 FROM {_table} WHERE {_key}=NEW.{_key})
BEGIN SELECT RAISE(ABORT, 'forgetting receipts are immutable'); END;
"""
    for _operation in ("UPDATE", "DELETE"):
        FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_no_{_operation.lower()}
BEFORE {_operation} ON {_table}
BEGIN SELECT RAISE(ABORT, 'forgetting receipts are immutable'); END;
"""

for _table in ("sessions", "messages", "turns"):
    _key = "id" if _table == "sessions" else "session_id"
    for _operation in ("UPDATE", "DELETE"):
        # The existing message triggers remain unconditional, including after forgetting.
        if _table == "messages":
            continue
        FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_forgotten_no_{_operation.lower()}
BEFORE {_operation} ON {_table}
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=OLD.{_key})
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""
    _parent = "parent_id" if _table == "sessions" else "session_id"
    FORGETTING_SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_forgotten_no_insert
BEFORE INSERT ON {_table}
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=NEW.{_parent})
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""

FORGETTING_SCHEMA += """
CREATE TRIGGER IF NOT EXISTS messages_no_replace
BEFORE INSERT ON messages
WHEN EXISTS (SELECT 1 FROM messages WHERE id=NEW.id
             OR (session_id=NEW.session_id AND seq=NEW.seq))
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;

CREATE TRIGGER IF NOT EXISTS sessions_forgotten_no_replace
BEFORE INSERT ON sessions
WHEN EXISTS (SELECT 1 FROM forgotten_sessions WHERE session_id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;

CREATE TRIGGER IF NOT EXISTS turns_forgotten_no_replace
BEFORE INSERT ON turns
WHEN EXISTS (SELECT 1 FROM turns t JOIN forgotten_sessions f ON f.session_id=t.session_id
             WHERE t.id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'session is forgotten'); END;
"""
