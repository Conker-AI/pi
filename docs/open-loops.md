# Open loops

One list, across every visible chat, of what the owner promised and what Conker promised. Code:
`pi/context/open_loops.py`, routes in `pi/api/routes/open_loops.py`.

A loop is one promise: its words (1-500 characters), who promised (`owner` or `conker`), an
optional due date (`YYYY-MM-DD` in the owner's time zone, the one the quiet hours use), the chat
it belongs to and, when it came from a message, that message (it must be one of the chat's own).
States are `open`, `done` and `dropped`; any of them can be set again, so a loop marked done by
mistake reopens. At most 1000 loops are open at once in the chats the owner can see; adding one
or opening one again past that is 409 `too_many`. A due date must be a real date before
9999-12-31 (422 `invalid_date`).

- `GET /open-loops?state=open|done|dropped|all&due_before=DATE&limit=N&after=ID` (owner
  credential; browser read): `LoopPage` with `today`, `timeZone`, the items and `nextAfter`.
  Soonest due first, undated last, then oldest first. Each item carries the chat's id and title,
  the first 300 characters of its message (`quote`), `overdue` (open and due before `today`) and a
  `revision`. `due_before` keeps only dated loops due before that day. A malformed `after` is
  422 `invalid_cursor`; one whose loop has since been forgotten is 422 `cursor_gone` (start
  again from the first page).
- `POST /open-loops` `{session_id, text, promised_by, due_on, message_id}`: file one loop.
- `POST /open-loops/{id}/state` `{state, expected_revision}`: done, dropped or open again. Without
  `expected_revision` it applies to whatever is current; with it, a stale one is 409
  `revision_conflict`. Setting the state it already has changes nothing.

The two writes take the recovery key for now; the browser gets them with the dashboard screen.

Why a table of its own and not a kind of the chat's context items ([decisions](decisions.md)):
a loop must survive its chat going `forked` (the automatic rollover) or `closed`, be listed
across chats, stay out of the chat's frozen settings snapshot (writing one must not pause a
queued message) and have its own cap.

Hidden chats: a loop in a forgotten or trashed chat, or in a branch of one, is neither listed nor
changed (404) until the chat is restored. Forgetting a chat deletes its loops
([forgetting](forgetting.md)).

Not yet: Conker filing its own promises (a native tool), suggesting the owner's from chat, and
the Morning check-in reading due loops. Design: context/design/open-loops.md in the Conker
workspace.
