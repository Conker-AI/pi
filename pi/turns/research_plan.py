"""Saving the public plan a deep-research turn starts with.

It writes the plan as an intermediate assistant message inside the turn's own transaction, after the
stop guard, so a stopped turn never saves a plan (pi.context.research holds the prompt and the limits).
"""

from __future__ import annotations

import json

from pi.context import research
from pi.conversation import submissions
from pi.turns import steering as turn_steering


def save_plan(store, turn_id, text, limit):
    try:
        value = json.loads(text)
        questions = value["research_plan"]
        if (
            set(value) != {"research_plan"}
            or not isinstance(questions, list)
            or not 1 <= len(questions) <= 5
            or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 300 for q in questions)
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise RuntimeError(
            "Research planner returned an invalid public plan; no search ran."
        ) from None
    search_limit = research.deep_search_limit(store, limit)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        turn_steering.guard_db(db, turn_id)
        turn = db.execute("SELECT session_id,status FROM turns WHERE id=?", (turn_id,)).fetchone()
        if (
            turn["status"] != "running"
            or db.execute("SELECT 1 FROM turn_cancellations WHERE turn_id=?", (turn_id,)).fetchone()
        ):
            raise RuntimeError("Research stopped before saving its plan.")
        submissions.append(
            db,
            turn["session_id"],
            "assistant",
            {"kind": "research_plan", "questions": questions, "search_limit": search_limit},
            turn_id=turn_id,
            purpose="intermediate",
        )
        db.commit()
