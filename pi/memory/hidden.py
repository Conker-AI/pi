"""Which MemoryGate records come from a hidden chat: the names memory's readers use for
pi.conversation.hidden_records, the one copy of that rule."""

from pi.conversation import hidden_records as _shared


def _receipt_ids(agent_id: str, message_id: str) -> tuple[str, str, str]:
    """MemoryGate's ids for one delivered message: evidence (the receipt id), analysis, memory."""
    return _shared.receipt_ids(agent_id, message_id)


def hidden_objects(db, agent_ids=(), own=None) -> dict[str, set[str]]:
    """MemoryGate records, by kind, that come from a hidden chat other than `own`; restoring the
    chat empties the set again. Recall and the owner's memory explorer both use it."""
    return _shared.hidden_records(db, agent_ids, own)
