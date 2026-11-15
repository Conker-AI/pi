"""The four starter templates seeded once into a new Pi database (R-F8, design note
context/design/prompt-templates.md). They are ordinary templates afterwards: the owner edits or
deletes them, and a deleted starter never comes back."""

from __future__ import annotations

STARTERS: tuple[dict, ...] = (
    {
        "id": "starter-research",
        "name": "Research",
        "description": "Find out about a subject and report back.",
        "fields": [
            {
                "id": "subject",
                "label": "Subject",
                "type": "longText",
                "required": True,
                "example": "Best laptop under $1000 for coding and travel",
            },
            {"id": "do", "label": "Do", "type": "list", "example": "Compare battery life"},
            {"id": "dont", "label": "Don't", "type": "list", "example": "Gaming laptops"},
            {
                "id": "output",
                "label": "Output",
                "type": "choice",
                "options": ["Report", "Table", "Slides", "File"],
            },
            {
                "id": "context",
                "label": "Context",
                "type": "longText",
                "example": "I write Python and travel by train",
            },
        ],
    },
    {
        "id": "starter-bug-report",
        "name": "Bug report",
        "description": "Describe something that broke so it can be found and fixed.",
        "fields": [
            {"id": "what", "label": "What happened", "type": "longText", "required": True},
            {"id": "expected", "label": "What you expected", "type": "longText"},
            {
                "id": "steps",
                "label": "Steps to reproduce",
                "type": "list",
                "example": "Open Settings, then Models",
            },
            {"id": "where", "label": "Where", "type": "shortText", "example": "Chat composer"},
            {
                "id": "severity",
                "label": "Severity",
                "type": "choice",
                "options": ["Low", "Medium", "High", "Blocking"],
            },
        ],
    },
    {
        "id": "starter-plan",
        "name": "Plan",
        "description": "Turn a goal into steps.",
        "fields": [
            {"id": "goal", "label": "Goal", "type": "longText", "required": True},
            {"id": "deadline", "label": "Finish by", "type": "date"},
            {"id": "constraints", "label": "Constraints", "type": "list"},
            {
                "id": "detail",
                "label": "Detail",
                "type": "choice",
                "options": ["Outline", "Step by step", "With owners and dates"],
            },
        ],
    },
    {
        "id": "starter-email",
        "name": "Email",
        "description": "Draft a message to someone in a chosen tone.",
        "fields": [
            {"id": "purpose", "label": "Purpose", "type": "longText", "required": True},
            {"id": "recipient", "label": "To", "type": "shortText"},
            {
                "id": "tone",
                "label": "Tone",
                "type": "choice",
                "options": ["Friendly", "Neutral", "Formal", "Firm"],
            },
            {
                "id": "length",
                "label": "Length",
                "type": "choice",
                "options": ["Short", "Medium", "Long"],
            },
            {"id": "include", "label": "Must include", "type": "list"},
        ],
    },
)
