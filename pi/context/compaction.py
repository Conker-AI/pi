"""Reviewed compaction folds messages into the summary; they are no longer sent to the model."""

from pydantic import Field

from pi.context import controls

SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_coverage (
 session_id TEXT NOT NULL REFERENCES sessions(id), revision INTEGER NOT NULL,
 message_id TEXT NOT NULL REFERENCES messages(id), PRIMARY KEY(session_id,revision,message_id)
);
"""


class Compact(controls.Strict):
    expected_policy_revision: int = Field(ge=1)
    expected_summary_revision: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=16000)
    covers: list[str] = Field(
        min_length=1, max_length=10000
    )  # distinct message ids, 1-200 chars each


def compact(store, sid, body):
    """Fold covered messages into the summary in one transaction."""
    body = Compact.model_validate(body.model_dump())
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            # 1. Chat exists, not forgotten, status open, not busy
            controls._session(db, sid)
            session = db.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
            if session["status"] != "open":
                raise controls.ContextError(
                    "session_closed", "Compaction requires an open conversation."
                )

            from pi.conversation import submissions

            if (
                submissions._busy(db, sid)
                or db.execute(
                    "SELECT 1 FROM turn_submissions WHERE requested_session_id=? AND state='preparing'",
                    (sid,),
                ).fetchone()
            ):
                raise controls.ContextError(
                    "session_busy", "Finish current work before compacting."
                )

            # 2. Policy exists and revision matches
            policy_row = db.execute(
                "SELECT revision,policy FROM context_policies WHERE session_id=?", (sid,)
            ).fetchone()
            if policy_row is None:
                raise controls.ContextError(
                    "no_settings", "Set context settings before compacting."
                )
            if policy_row[0] != body.expected_policy_revision:
                raise controls.ContextError(
                    "revision_conflict", "Context policy changed; reload before compacting."
                )

            # Summary revision must match
            from pi.context import summaries

            current = summaries._current(db, sid)
            if current["revision"] != body.expected_summary_revision:
                raise controls.ContextError(
                    "revision_conflict", "Summary changed; reload before compacting."
                )

            # 3. Validate covers
            if len(set(body.covers)) != len(body.covers):
                raise controls.ContextError(
                    "duplicate", "Message IDs in covers must be distinct.", 422
                )

            # Check each cover message
            policy = controls.Policy.model_validate_json(policy_row[1])
            for message_id in body.covers:
                # Check message belongs to this chat or inherited
                message = db.execute(
                    "SELECT session_id FROM messages WHERE id=?", (message_id,)
                ).fetchone()
                inherited = db.execute(
                    "SELECT 1 FROM context_inherited_messages WHERE session_id=? AND message_id=?",
                    (sid, message_id),
                ).fetchone()
                if message is None or (message[0] != sid and not inherited):
                    raise controls.ContextError(
                        "outside_boundary", "Message does not belong to this conversation.", 422
                    )

                # Check current rule
                current_rule = policy.messagePolicies.get(message_id)
                if current_rule == "keep-exact":
                    raise controls.ContextError("pinned", "Pinned messages are never folded.")
                if current_rule == "retrieve":
                    raise controls.ContextError(
                        "retrieve_rule", "Clear the retrieve rule before folding this message."
                    )

            # 4. Write changes
            # Insert revision-0 baseline if needed
            if current["revision"] == 0:
                import time

                db.execute(
                    "INSERT OR IGNORE INTO context_summary_versions VALUES(?,0,?,?,?,NULL)",
                    (sid, current["summary"], current["source_session_id"], time.time()),
                )

            # Insert new summary version
            import time

            revision = current["revision"] + 1
            db.execute(
                "INSERT INTO context_summary_versions VALUES(?,?,?,?,?,?)",
                (sid, revision, body.summary, current["source_session_id"], time.time(), None),
            )

            # Update session summary
            db.execute("UPDATE sessions SET summary=? WHERE id=?", (body.summary, sid))

            # Insert coverage rows
            for message_id in body.covers:
                db.execute(
                    "INSERT INTO summary_coverage VALUES(?,?,?)",
                    (sid, revision, message_id),
                )

            # Update policy rules for covered messages
            updated_policy = policy.model_copy()
            for message_id in body.covers:
                if updated_policy.messagePolicies.get(message_id) is None:
                    updated_policy.messagePolicies[message_id] = "allow-summary"

            db.execute(
                "INSERT INTO context_policies VALUES (?,?,?) ON CONFLICT(session_id) "
                "DO UPDATE SET revision=excluded.revision,policy=excluded.policy",
                (sid, policy_row[0] + 1, updated_policy.model_dump_json()),
            )

            db.commit()

            # Return result
            covered_set = set(body.covers)
            folded = [
                message_id
                for message_id in body.covers
                if updated_policy.messagePolicies.get(message_id) == "allow-summary"
            ]

            return {
                "policyRevision": policy_row[0] + 1,
                "summaryRevision": revision,
                "covered": sorted(covered_set),
                "folded": sorted(folded),
            }
        except Exception:
            db.rollback()
            raise


def covered(db, sid):
    """Return the current coverage as a set of message IDs."""
    # Get current summary version
    current = db.execute(
        "SELECT revision,restored_from FROM context_summary_versions "
        "WHERE session_id=? ORDER BY revision DESC LIMIT 1",
        (sid,),
    ).fetchone()

    if current is None:
        return set()

    # If restored, follow the chain
    revision = current[0]
    restored_from = current[1]
    if restored_from is not None:
        # Find the coverage of the restored version
        restored_coverage = db.execute(
            "SELECT message_id FROM summary_coverage WHERE session_id=? AND revision=?",
            (sid, restored_from),
        ).fetchall()
        return {row[0] for row in restored_coverage}

    # Get coverage for current revision
    coverage = db.execute(
        "SELECT message_id FROM summary_coverage WHERE session_id=? AND revision=?",
        (sid, revision),
    ).fetchall()
    return {row[0] for row in coverage}


def view(store, sid):
    """Return current compaction state."""
    with store._connect() as db:
        controls._session(db, sid)

        # Get current summary revision
        current = db.execute(
            "SELECT revision FROM context_summary_versions "
            "WHERE session_id=? ORDER BY revision DESC LIMIT 1",
            (sid,),
        ).fetchone()
        summary_revision = current[0] if current else 0

        # Get covered messages
        covered_set = covered(db, sid)

        # Get current policy to find folded messages
        policy_row = db.execute(
            "SELECT policy FROM context_policies WHERE session_id=?", (sid,)
        ).fetchone()
        folded_set = set()
        if policy_row and policy_row[0]:
            policy = controls.Policy.model_validate_json(policy_row[0])
            for message_id, rule in policy.messagePolicies.items():
                if rule == "allow-summary" and message_id in covered_set:
                    folded_set.add(message_id)

        return {
            "summaryRevision": summary_revision,
            "covered": sorted(covered_set),
            "folded": sorted(folded_set),
        }
