# Output settings

Output settings control how the model formats its answers in a chat. There are three settings: format, length, and language. Each can be set to "auto" to let the model decide, or to a specific value to guide the model's response.

**Format** determines the structure of the answer. Options are: auto (no instruction), plain prose, a list, a table, a structured document with headings, code with only brief explanation, or only valid JSON.

**Length** controls how detailed the answer should be. Options are: auto (no instruction), short (a few sentences), medium (a few paragraphs), or long and thorough.

**Language** specifies the output language using BCP 47 language tags like "en", "en-GB", or "ru". Use "auto" to let the model choose.

These settings are saved per chat with a revision number to prevent conflicts. GET/POST `/sessions/{id}/output` read and change them (admin key only for now). When a chat is forked, the child session inherits the parent's output settings automatically.

Each turn freezes the current output settings in its execution snapshot, so the model's behavior remains consistent even if settings change later. The settings are sent to the model as system instructions, not strict guarantees—the model may still choose a different format or style if it better serves the request.
