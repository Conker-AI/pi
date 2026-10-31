# Chat suggestions

Jev, the routing helper, can suggest 1 to 3 messages the owner might send next in one chat. The
owner asks for them (the dashboard after an answer, while Suggest your next message is on and
Suggestion style chooses how they look); nothing runs by itself.

The route is `POST /sessions/{id}/suggestions` with an optional `count` from 1 to 3. The default
is the owner's Suggestions shown setting (`chats.suggestions_count`), or 2 when it is not set.
The answer carries `messageId` (the last answer the suggestions follow), `suggestions` and
`modelId`.

What is sent to the helper: the last answer and the owner's message before it, each cut to 2000
characters. Excluded, redacted and forgotten messages are never sent. Nothing is saved.

The route refuses: an unknown chat (404 `not_found`), helpers off for the chat (409
`helpers_off`), no answer to suggest from (409 `empty`), no routing model (503 `not_configured`),
and a reply Jev could not give (503 `unavailable`).

The reply is untrusted text: an item with a control or direction-changing character is dropped,
the rest are trimmed, capped at 120 characters and kept once each. The owner key may call the
route; the dashboard decides whether to show the suggestions.
