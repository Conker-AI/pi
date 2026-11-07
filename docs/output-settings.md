# Output settings

Output settings control how the model formats its answers in a chat. There are four settings: format, length, language, and level. Each can be set to "auto" to let the model decide, or to a specific value to guide the model's response.

**Format** determines the structure of the answer. Options are: auto (no instruction), plain prose, a list, a table, a structured document with headings, code with only brief explanation, or only valid JSON.

**Length** controls how detailed the answer should be. Options are: auto (no instruction), short (a few sentences), medium (a few paragraphs), or long and thorough.

**Language** specifies the output language using BCP 47 language tags like "en", "en-GB", or "ru". Use "auto" to let the model choose.

**Level** sets how short and simple the answers are. Options are:

- auto — follow the owner's `chats.language_level` setting in the owner settings document (a missing or unknown value there means normal)
- strict — a controlled, simplified technical style: short sentences of about 20 words at most, one idea or one instruction per sentence, active voice, common words, one word for one meaning used the same way every time, no idioms, slang or figures of speech, and steps written as numbered imperative sentences
- plain — plain everyday words, short sentences and paragraphs, with any technical term explained in a few words when it is needed
- normal — no instruction at all; the default
- deep — full detail with precise technical terms: show the reasoning and the trade-offs, and cover the edge cases

Normal adds no instruction to the model; the other levels are sent as one "Language level" sentence in the chat's output settings system message.

These settings are saved per chat with a revision number to prevent conflicts. GET/POST `/sessions/{id}/output` read and change them (admin key only for now). When a chat is forked, the child session inherits the parent's output settings automatically.

Each turn freezes the current output settings in its execution snapshot, so the model's behavior remains consistent even if settings change later. The settings are sent to the model as system instructions, not strict guarantees—the model may still choose a different format or style if it better serves the request.
