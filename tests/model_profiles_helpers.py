"""Builders for the model-profile tests: models, turns, response families, judgements and votes."""

import itertools
import json
import time

from pi.models import roles as model_roles
from tests.test_model_roles import config

# A counter, not the clock: time.time_ns() repeats on Windows between two quick calls.
_judgement_ids = itertools.count()


def setup_models(store):
    """Setup model roles with models a, b."""
    value = config()
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def create_turn(
    store,
    session_id,
    model_route,
    status="complete",
    latency_ms=None,
    cost_usd=None,
    input_tokens=None,
    output_tokens=None,
):
    """Create a turn with specified metrics using proper Store API."""
    # Start the turn
    turn_id = store.start_turn(session_id)

    # Prepare fields for the turn
    turn_fields = {
        "model": model_route,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    # Remove None values
    turn_fields = {k: v for k, v in turn_fields.items() if v is not None}

    if status == "complete":
        # Complete the turn with a test message and metrics
        store.complete_turn(turn_id, "test", **turn_fields)
    else:
        # For non-complete status, we need to use finish_turn directly with a message first
        # Create the message manually since we're not using complete_turn
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Create the message first
            from pi.conversation import submissions

            submissions.append(
                db, session_id, "assistant", "test", turn_id=turn_id, purpose="final"
            )
            # Then finish the turn with the desired status and metrics
            store._finish_turn(db, turn_id, status, **turn_fields)
            db.commit()

    return turn_id


def create_response_family(store, session_id, message_a_id, message_b_id):
    """Create a response family with two versions for testing judgements and votes."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Create response family with message_a as root
        root = message_a_id
        db.execute("INSERT INTO response_families VALUES(?,?,?,1)", (root, session_id, root))
        db.execute("INSERT INTO response_versions VALUES(?,?,NULL)", (root, root))
        # Add message_b as version of root
        db.execute(
            "INSERT INTO response_versions VALUES(?,?,?)", (message_b_id, root, message_a_id)
        )
        db.commit()
    return root


def create_judgement(store, session_id, root_message_id, scores, winner, request_id=None):
    """Create a judgement with scores."""
    if request_id is None:
        request_id = f"req_{next(_judgement_ids):016x}"
    result = {
        "request_id": request_id,
        "judgeModelId": "judge-model",
        "rubric": "",
        "scores": scores,
        "winner": winner,
        "excluded": [],
        "created_at": time.time(),
    }
    with store._connect() as db:
        db.execute(
            "INSERT INTO response_judgements (request_id, session_id, root_message_id, payload_hash, judge_model_id, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                session_id,
                root_message_id,
                "hash",
                "judge-model",
                json.dumps(result),
                time.time(),
            ),
        )
        db.commit()
    return request_id


def create_vote(store, session_id, root_message_id, message_id, vote):
    """Create an owner vote."""
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO response_votes (message_id, session_id, root_message_id, vote, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, session_id, root_message_id, vote, time.time()),
        )
        db.commit()
