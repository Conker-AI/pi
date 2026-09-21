"""Research selections are immutable intent, never authority or evidence of execution."""

from . import tasks


def validate(mode, snapshot=None):
    if not isinstance(mode, str) or mode not in ("off", "web", "deep"):
        raise tasks.TaskError("invalid_research_mode", "Select off, web or deep research.", 422)
    if mode != "off" and snapshot and (
        snapshot.get("callExecution") or snapshot.get("kind") == "team-role"
    ):
        raise tasks.TaskError(
            "research_scope", "Research selection currently belongs to ordinary conversations.", 422
        )


def require_runtime(execution):
    """Fail before retrieval/model calls until the bounded research executor exists."""
    mode = execution.get("researchMode", "off")
    validate(mode, execution)
    if mode != "off":
        raise tasks.TaskError(
            "research_unavailable",
            "Research execution is not configured. Your requested mode is retained; "
            "no search or answer generation was started.",
            503,
        )
