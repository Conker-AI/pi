# Output settings

Output settings control how the model formats its answers in a chat. There are five settings: format, length, language, level, and effort. Each can be set to "auto" to let the model decide, or to a specific value to guide the model's response.

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

**Effort** sets how hard the answering model thinks: auto, light, medium, high or max. It is sent to the one answer call only (never to routing or any other helper role), mapped to each provider's own control, and a provider or model with no control for it ignores it: nothing is sent and nothing fails. Thinking tokens are billed like any other output, so the daily spending cap counts them. Auto adds nothing (the provider's own default), and an Auto proposal may set it with a reason.

| Provider | Effort reaches the model when |
| --- | --- |
| OpenAI | the model is a reasoning model (o-series, gpt-5 but not its chat variants); `reasoning_effort` low/medium/high, max is high |
| OpenRouter | the catalogue entry lists `reasoning` as a supported parameter; `reasoning.effort`, max is xhigh |
| Anthropic | the model's own capability list (`GET /v1/models/{id}`) supports effort; `output_config.effort`, max falls back to high |
| Ollama | the model is gpt-oss; `think` low/medium/high, max is high |
| Gemini, xAI, Mistral, DeepSeek, Groq, ChatGPT plan, Claude plan | ignored |

The settings reply carries `effortSupport`: one row `{modelId, providerId, applies}` per model that can answer this chat, where `applies` is true, false, or null when Pi learns it only when it asks the provider (a Claude model's capability list, an OpenRouter model's parameters). A chat's snapshot holds `effort` only when it is not auto, so older snapshots are unchanged. The list covers the models that may answer this chat (the chat's agent model when it pins one); a model the owner picks for a single message is not listed. The deep-research planning call is not the answer and never takes effort. Calls and non-companion turns never take it.

These settings are saved per chat with a revision number to prevent conflicts. A save replaces the whole document, so a client that leaves a setting out (an older dashboard that does not know `effort`) saves it as auto. GET/POST `/sessions/{id}/output` read and change them (admin key only for now). When a chat is forked, the child session inherits the parent's output settings automatically.

Each turn freezes the current output settings in its execution snapshot, so the model's behavior remains consistent even if settings change later. The settings are sent to the model as system instructions, not strict guarantees—the model may still choose a different format or style if it better serves the request.
