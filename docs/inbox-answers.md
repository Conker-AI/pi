# Inbox answers

The owner answers in his Inbox, often once or twice a day. His answer continues the work; it does
not wait for him to open the chat.

## Decisions on requests

After ToolGate records his decision on a request, the gateway calls `POST
/approvals/{request_id}/decided` with `{status, note}` (owner credential). This happens from the
browser's Inbox route and from `conker apply approvals`. Pi finds what waits on that request: a
chat turn, an AI job's turn, or a tool cron job's run.

- **approved** (Do it): the parked turn resumes now, in the background. Pi replays the exact
  stored action; ToolGate checks the approval as it is spent, so Pi grants nothing. A tool cron
  job's run resumes the same way.
- **rejected / dismissed** (Don't): the turn ends `cancelled` and the action never runs. Pi
  records a note beside the tool's id that he said no. The note is Pi's own text, kept outside
  any tool result, so it is not outside content. The chat takes messages again. A tool cron job's
  run is cancelled.
- **rejected with a note** (Change): as Don't, then his note becomes his next message in that chat
  and starts a new turn. Whatever Conker does next passes every access layer again; his typed note
  counts as his own words.

The answer is `{"continued": "running" | "closed" | "your_words" | "nothing_waiting", ...}`.
`nothing_waiting` means nothing waits on that request any more, for example because the chat
already resumed it. The gateway ignores Pi's answer: the decision stands in ToolGate, and opening
the chat still resumes a turn that Pi could not continue.

Declining one of Conker's requests can carry his words too: `POST /change-requests/{id}/decline`
with `{"reply": "..."}` sends them to that chat as his next message.

## Conker's questions

`conker.ask` (one of Conker's own tools) asks him a question with 2 to 4 options that Conker
writes; Yes / No are used when it gives none that fit. The turn that asked ends at once, and the
question waits in his Inbox (`GET /asks`; also listed in `GET /inbox` under `needsYou` as kind
`ask`). Asking is always allowed: it is the safe path. At most 3 questions wait per chat and 20 in
all.

- `POST /asks/{id}/answer` with `{text, chosen}` records his answer once. It goes to that chat as
  his message, `About “<question>”: <answer>`, and starts a new turn. If the chat is busy, Pi
  waits for it (up to five minutes).
- `chosen: true` means he tapped one of the options shown (anything else is refused). Options are
  the AI's words, perhaps written after reading someone else's text, so they never count as his
  own request approving a matching action. Only what he types does.
- `POST /asks/{id}/dismiss` closes the question; nothing goes back to the chat.

Answering and dismissing need only the signed-in browser session, like typing in the chat. Any
action that follows passes his access layers. Forgetting a chat removes its questions. The
`conker` CLI does the same: `conker inspect asks` and `conker apply asks` with
`{"operation": "answer" | "dismiss", "id", "text", "chosen"}`.
