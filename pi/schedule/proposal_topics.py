"""Topics the owner never wants ideas about (`proactivity.skip_topics` in his settings document).

The proposals pass shows them to its model, and an idea that still names one (in its title, what was
noticed, the suggestion or what would happen) is dropped, so a model that ignores the list adds
nothing about them. A topic matches ignoring case, anywhere in those words. The setting is a
preference: a value that is not a list of text counts as no topics.
"""

from __future__ import annotations

from pi.core import owner_settings as owner_settings

SETTING = "proactivity.skip_topics"
MAX_TOPICS = 50  # the registry's max_items
MAX_TOPIC = 100
FIELDS = ("title", "noticed", "suggestion", "if_approved")


def read(db) -> list[str]:
    """The owner's topics, trimmed, inside the caller's transaction; empty when none are set."""
    value = owner_settings.value_in(db, SETTING, [])
    if not isinstance(value, list):
        return []
    topics = [" ".join(item.split())[:MAX_TOPIC] for item in value if isinstance(item, str)]
    return [topic for topic in topics if topic][:MAX_TOPICS]


def listing(topics: list[str]) -> str:
    """The block that follows the declined list in the pass's prompt; nothing when none are set."""
    if not topics:
        return ""
    return "\n\nTopics to skip:\n" + "\n".join(f"- {topic}" for topic in topics)


def names_one(idea: dict, topics: list[str]) -> bool:
    """Whether a parsed idea mentions any of the topics."""
    words = " ".join(str(idea.get(field) or "") for field in FIELDS).casefold()
    return any(topic.casefold() in words for topic in topics)
