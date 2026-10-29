"""Forgetting a chat must reach every table that can hold its text.

Each table is either named by the forgetting code (forgetting.py or a redact helper) or listed
here with the reason it never holds chat text. A new table that is neither fails this test, so a
feature cannot quietly keep what the owner forgot. test_forgetting*.py prove the clearing itself.
"""

import ast
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from pi.conversation.store import Store

ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "redact",
    "_redact",
    "_scrub",
    "invalidate_recall",
    "redact_recall",
    "redact_sources",
    "delete_purged_files",
}

# Identifiers, states, counts and costs: no text from a message.
IDS_ONLY = {
    "activity_events",
    # A fingerprint of a webhook secret and when it was last used; never the secret.
    "agent_job_hooks",
    # A fingerprint of a watched tool's answer and when it was read; never the answer.
    "agent_job_watch",
    "attachment_removals",
    "attachment_reservations",
    "call_events",
    "context_fork_requests",
    "context_inherited_messages",
    "continuity_ack",
    "continuity_delivered",
    "continuity_migrations",
    "continuity_order",
    "continuity_seen",
    "draft_starts",
    "filesystem_reads",
    "memory_forget_operations",
    "message_attachment_sets",
    "message_attachments",
    "message_fork_requests",
    "message_privacy",
    "proactive_reservations",
    "proposal_watermark",
    "research_model_calls",
    "response_families",
    "response_retries",
    "response_versions",
    "scheduled_run_budgets",
    # A service's model call: service, role, model, usage. Pi never keeps its prompt or answer.
    "service_model_calls",
    "session_observations",
    "steering_discarded_answers",
    "system_actions",
    "system_inventory_identity",
    "system_inventory_reads",
    "task_runs",
    "team_model_calls",
    "turn_cancellations",
    "turn_messages",
    "turn_reply_recoveries",
    "turn_steering_controls",
}

# What the owner configured, not what a chat said. Team runs keep their configuration here;
# their messages live in child sessions of the source chat and are forgotten with it. The
# new-chat draft is emptied when the chat starts.
OWNER_CONFIGURATION = {
    "agent_versions",
    "character_profiles",
    "character_versions",
    "collaboration_preparations",
    "collaboration_records",
    "model_evaluation_case_versions",
    "model_evaluation_cases",
    "model_evaluation_runs",
    "model_role_settings",
    "new_conversation_draft",
    "owner_preferences",
    "owner_search_settings",
    # Settings by registry id (look, defaults, limits): chosen by the owner, never chat text.
    "owner_settings",
    "projects",
    "scheduled_jobs",
    "scheduled_runs",
    "setup_companion_choices",
    "setup_evidence_receipts",
    "setup_model_probe_receipts",
    "setup_model_probe_requests",
    "setup_optional_choices",
    "setup_protection_policies",
    "setup_rehearsal_approvals",
    "setup_rehearsal_memory_reviews",
    "team_definition_revisions",
    "team_runs",
    "team_steps",
    "template_publications",
}

# Readings from the subscription runtimes: plan names, window labels and percentages, no chat.
PROVIDER_READINGS = {"plan_alerts", "plan_usage"}


def tables(tmp_path):
    with closing(Store(tmp_path / "pi.db")), closing(sqlite3.connect(tmp_path / "pi.db")) as db:
        return {
            name
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def forgetting_code():
    text = (ROOT / "pi/conversation/forgetting.py").read_text(encoding="utf-8")
    for path in (ROOT / "pi").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
                text += ast.get_source_segment(source, node) or ""
    return text


def test_every_table_is_forgotten_or_holds_no_chat_text(tmp_path):
    present = tables(tmp_path)
    code = forgetting_code()
    named = {name for name in present if re.search(rf"\b{re.escape(name)}\b", code)}
    listed = IDS_ONLY | OWNER_CONFIGURATION | PROVIDER_READINGS

    unclassified = sorted(present - named - listed)
    assert not unclassified, (
        "Clear these tables when a chat is forgotten, or list them here with the reason they "
        f"never hold chat text: {unclassified}"
    )
    assert not sorted(listed - present), "Remove tables that no longer exist from the lists."
    assert not sorted(listed & named), "A table the forgetting code clears needs no listing."
    assert not IDS_ONLY & OWNER_CONFIGURATION
