"""Which MemoryGate records come from a hidden chat (pi.conversation.visibility), found from
Pi's own tables without asking MemoryGate."""

from uuid import NAMESPACE_URL, uuid5

from pi.conversation import visibility


def _receipt_ids(agent_id: str, message_id: str) -> tuple[str, str, str]:
    """MemoryGate's ids for one delivered message (the formula in delivery.validate_receipt):
    the evidence record is the receipt id; its analysis and its memory derive from it."""
    receipt = str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))
    analysis = str(uuid5(NAMESPACE_URL, receipt + ":analysis"))
    return receipt, analysis, str(uuid5(NAMESPACE_URL, receipt + ":memory"))


def hidden_objects(db, agent_ids=()) -> dict[str, set[str]]:
    """MemoryGate records, by kind, that come from a hidden chat (pi.conversation.visibility).

    Each message Pi delivered becomes evidence, an analysis and maybe a memory whose ids derive
    from the message id and the receiving agent, so they are found without asking MemoryGate.
    A memory Conker kept on its own carries no chat at all: keep_results names it. Recall and
    the owner's memory explorer both use this one set; restoring the chat empties it again.
    """
    found = {
        "memory": visibility.kept_from_hidden(db, "memory"),
        "evidence": set(),
        "analysis": set(),
    }
    rows = db.execute(
        "SELECT m.id, o.destination_agent_id FROM messages m "
        "LEFT JOIN memory_outbox o ON o.message_id=m.id "
        f"WHERE {visibility.hidden_sql('m.session_id')}"
    ).fetchall()
    for message_id, destination in rows:
        for agent in {destination, *agent_ids} - {None, ""}:
            evidence, analysis, memory = _receipt_ids(agent, message_id)
            found["evidence"].add(evidence)
            found["analysis"].add(analysis)
            found["memory"].add(memory)
    found["memory"] |= _rewritten_from_hidden(db)
    return found


def _rewritten_from_hidden(db) -> set[str]:
    """Memories a hidden chat rewrote through a memory proposal (pi.memory.proposals).

    The memory keeps its id but its text came from that chat, so it is hidden with the chat
    while the edit was applied or may have been (applying, unknown); a pending, rejected or
    failed proposal changed nothing. Forgetting scrubs the row to `forgotten`, so a forgotten
    chat's edit is no longer visible here.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_proposals'").fetchone():
        return set()
    rows = db.execute(
        "SELECT memory_id FROM memory_proposals "
        "WHERE state IN ('applied','applying','unknown') "
        f"AND {visibility.hidden_sql('session_id')}"
    )
    return {row[0] for row in rows}
