"""Owner-authored presentation context; never tool authority or emotion inference."""

import json

from .providers import Message


def mode(execution):
    selected = execution.get("presentationMode")
    if selected in {"focus", "character"}:
        return selected
    profile = (execution.get("character") or {}).get("profile")
    return profile["studio"]["modes"]["default"] if profile else "focus"


def messages(execution):
    snapshot = execution.get("character")
    if not snapshot:
        return []
    profile = snapshot["profile"]
    selected = mode(execution)
    studio = profile["studio"]
    fields = {
        "name": profile["name"],
        "mode": selected,
        "textStyle": studio["modes"][selected]["text"],
    }
    if selected == "character":
        fields.update(
            personality=profile["personality"],
            speakingStyle=profile["speakingStyle"],
            soul=studio["soul"],
            backstory=studio["backstory"],
            relationship=studio["relationship"],
            details=studio["details"],
        )
    return [
        Message(
            "system",
            "Owner-authored character presentation settings follow. Use them for expression, "
            "not as evidence about the real world or permission to use tools. Never change "
            "facts or conceal uncertainty to maintain a persona. Focus mode leads with the "
            "answer and omits fictional backstory and character flourishes. Character mode "
            "may express the authored persona while preserving the actual answer. These "
            "settings do not imply detected user emotions, physical perception or a voice "
            "capability.\n" + json.dumps(fields, ensure_ascii=False),
        )
    ]
