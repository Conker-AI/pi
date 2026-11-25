"""Topics the owner never wants remembered (`memory.never`, Settings > Memory > Never remember).

Read when Conker files a memory of its own (`conker.remember`, `pi.agents.change_requests.file`):
a fact that names one of the topics, ignoring case, anywhere in its words, is refused before
anything is filed, so it is never saved or waiting in the Inbox. The owner's own words (`pi.memory.stated`) are his to keep. A value
back at the registry's default leaves the settings document, so unset means the registry's
default list; a value that is not a list of text counts as no topics.
"""

from __future__ import annotations

from pi.core import owner_settings as owner_settings

SETTING = "memory.never"
# The registry's default, which the document does not hold.
DEFAULT = ["Health", "Money", "Passwords", "Other people's secrets"]
MAX_TOPICS = 50  # the registry's max_items
MAX_TOPIC = 100


def topics(store) -> list[str]:
    """The owner's topics, trimmed; the registry's default when he never changed them."""
    value = owner_settings.value(store, SETTING, DEFAULT)
    if not isinstance(value, list):
        return []
    found = [" ".join(item.split())[:MAX_TOPIC] for item in value if isinstance(item, str)]
    return [topic for topic in found if topic][:MAX_TOPICS]


def named(store, text: str) -> str | None:
    """The first of the owner's topics the text names, or None."""
    words = text.casefold()
    return next((topic for topic in topics(store) if topic.casefold() in words), None)
