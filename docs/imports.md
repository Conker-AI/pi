# Importing chats from ChatGPT and Claude

The owner can bring their history from another assistant: Pi reads a ChatGPT or Claude data
export, shows what it would add, and on the owner's yes keeps each chat as an archived,
read-only chat and offers the owner's own words to MemoryGate as outside content. Nothing runs
during an import: no model, no tool, no turn, no Conker.

Code: `pi/memory/import_formats.py` (the readers), `pi/memory/imports.py` (preview, import,
archive), `pi/memory/import_offers.py` (offers to MemoryGate), `pi/api/routes/imports.py`
(routes). CLI: `conker import chatgpt|claude PATH --preview|--yes` (Conker repository).

## Which file

- **ChatGPT**: Settings, Data controls, Export data. The emailed zip holds `conversations.json`.
- **Claude**: Settings, Privacy, Export data. The emailed zip holds `conversations.json`.

Pi takes `conversations.json` itself as the request body. The CLI reads it from the zip, a folder
or the file; Pi never opens a zip, so the images and files in an export never reach it.

## What is kept

For ChatGPT, a chat is the path from its `current_node` back to its first message: what the chat
showed, not branches left behind by an edit. For Claude, the `chat_messages` in order. Pi keeps
what either side said: the text of user and assistant messages, a mark like `[image]` or
`[file: notes.txt]` where something was attached, the chat's title, its id and times from the
export, and each message's id and time. It leaves out (and counts) tool calls and results,
browsing and code runs, reasoning, hidden notes such as custom instructions and the other
assistant's own memory, and an attachment's extracted text.

## Strict reading

The file is UTF-8 JSON holding a list of chats, read one chat at a time.

| Limit | Value | Over it |
|---|---|---|
| File size | 128 MB | refused (`too_large`, 413) |
| Chats in the file | 20,000 | refused (`too_many_chats`) |
| Messages in the file | 500,000 | refused (`too_many_messages`) |
| Messages in one chat | 10,000 | that chat is left out (`longChats`) |
| Message length | 50,000 characters | cut, with a note saying how much was left out |
| Title | 300 characters | cut |

A file that is not a list, not valid JSON (NaN and Infinity included), or has anything after the
list is refused whole (`not_a_list`, `not_json`, 422). A chat without the expected shape (wrong
types, a missing or looping `current_node` path, an id outside `[A-Za-z0-9_.:-]`, a time outside
1970..2100) is left out and counted, never guessed at; when no chat matches, the file is refused
(`wrong_format`), naming the other export if it looks like one. NUL characters are dropped and
unpaired surrogates become U+FFFD.

## Routes

All are owner routes: the owner credential (`X-Pi-Owner-Key`, on the owner allowlist) or the
recovery key. The browser reaches them through the gateway ([browser-auth.md](browser-auth.md)):
a preview with the owner's session, an import with the password.

| Route | Does |
|---|---|
| `POST /imports/{chatgpt\|claude}/preview` | Reads the body; saves nothing. Counts (chats new, updated, unchanged; messages new, the owner's, offered to memory), first and last time, up to five sample titles, what was left out, whether memory is on and the listener set up, and the file's `digest`. |
| `POST /imports/{chatgpt\|claude}/{digest}` | Imports the same file. A body with another SHA-256 is refused (`file_changed`, 409), so the yes is for exactly the file previewed. |
| `GET /imports` | The latest imports (newest first) and the memory offers: `waiting`, `offered`, `refused`, and `problem` in plain words. |
| `GET /imports/chats?source=&limit=&offset=` | Imported chats, latest first: `label` ("Imported from ChatGPT"), `title`, `exportId`, times, `messageCount`, `archived: true`, `readOnly: true`. |
| `GET /imports/chats/{id}?after=&limit=` | One chat and a page of messages (`author` user or assistant, `text`, `createdAt`, `shortened`, `exportId`, and for the owner's messages `memory`: offering, offered, refused or not_offered). |

## Read-only, and never the owner's words

Imported chats live in their own tables (`imported_chats`, `imported_messages`), not in Pi's
conversation store, and an imported message cannot be changed. No turn can be sent to one, and
no fork, search, summary, recall or access check reads them, so imported text can never be
the owner's own message in an access decision (`pi.turns.access.owner_text`). The only way it can
reach a turn is through MemoryGate recall, and recalled memories make a turn count as having read
outside content (`pi.turns.access.outside_content`), so the owner's words in that turn no longer
loosen an action that would ask.

## Idempotent

A chat is known by its source and the export's chat id, a message by its chat and the export's
message id. Importing the same file again adds nothing; a later export of a chat that went on
adds only the new messages, after the ones already kept. Each chat is saved whole or not at all,
so an import that stopped is finished by importing the same file again.

## Memory: outside content through MemoryGate's listener

Only messages the owner wrote in the other assistant are offered, whole and at most 16,000
characters; the other assistant's replies stay in the archive. Nothing is offered when memory was
skipped at setup or new chats start private (`chats.default_incognito`).

MemoryGate's conversation route would admit them as owner statements and its statement route
keeps words the owner approved or Conker wrote, so neither is used. Offers go to a MemoryGate
listener, its ingress for outside sources: each becomes evidence of the listener's source type and
goes through MemoryGate's normal admission; a memory it keeps is an `automatic_listener` memory.
Each event is titled "Imported from ChatGPT: <chat title>", tagged `imported` and the source, sent
with integrity confidence 0.5, and its payload says `origin: "import"`, `author: "user"`, the
chat and message ids, and that the words were not said to Conker nor approved as a memory.

Set up once: in MemoryGate's Sources & Evidence add a listener (for example source key
`assistant-imports`, type `imported_chat`) whose public config sets `agent_id` to Pi's
`PI_MEMORYGATE_AGENT_ID`, rotate its key, then give Pi:

```dotenv
PI_MEMORYGATE_IMPORT_SOURCE=assistant-imports
PI_MEMORYGATE_IMPORT_KEY=<the listener key>
```

Both or neither, with `PI_MEMORYGATE_URL`; one without the other stops Pi at startup. Without
them an import still keeps the chats, and its offers wait until the listener is set up.

A background sender (started by the router's lifespan) sends one event at a time. A refused
listener (401, 403, 404, 429) pauses every offer for a minute and says why; another 4xx refuses
that message only (it stays in the archive); no answer or an invalid one is retried with backoff
up to five minutes. A listener event has no request id, so an answer lost after MemoryGate saved
the event is sent again: MemoryGate keeps one memory per exact text, so a repeat adds evidence,
not a second memory.

## Not yet

- Gateway routes and the dashboard screens (upload, preview, confirm; imported chats in Chats).
- Removing an imported chat, and taking its offers back from MemoryGate: Pi holds no key that can
  invalidate listener evidence. The owner can forget those memories in MemoryGate.
- OpenClaw and Hermes folders, skills and cron jobs from other assistants.
- A MemoryGate import origin on its conversation route (idempotent by id, forgettable by Pi)
  would replace the listener.
