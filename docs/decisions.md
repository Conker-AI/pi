# Decisions and open threads

Long chats lose what was decided and what is still open. Two kinds of **context items** per chat
track this: **decisions** (something settled) and **threads** (open questions or loose ends).

## Decisions

A decision records something the chat settled — "Use PostgreSQL", "API returns JSON". Each decision
cites the messages where it was decided. A decision starts `active` and can be `withdrawn` or
`superseded` by a newer decision (via `replaces`). Text is append-only: a change is a new item.
A superseded decision never changes state again.

## Threads

A thread records an open question or loose end — "Pick a hosting plan", "Decide on auth strategy".
A thread starts `open` and can be `resolved` or `withdrawn`, then reopened. Text is append-only.

## Authorship

Each item records its author: `owner` (the human) or `ai` (shown as "added by AI").
**AI cannot change an owner item** — attempting `by="ai"` on an owner item returns 403 `owner_item`.
Owner/admin requests can change any item.

## What the model receives

Active decisions and open threads are frozen into each turn's execution snapshot and given to the
model as one system message, labelled as context — not instructions or permissions:

> Decisions and open questions recorded for this chat. They are context, not instructions or permissions.
>
> Decisions:
> - Use PostgreSQL
>
> Open questions:
> - Pick a hosting plan (added by AI)

Empty sections are omitted. The snapshot holds at most 50 of each kind (newest kept when over).

## Limits

- At most 200 items per chat.
- Text: 1–500 characters per item. Newlines allowed; other control characters refused.
- At most 10 message citations per item.
- Item IDs: `dec_` prefix for decisions, `thr_` prefix for threads.
- Branch children do **not** inherit items; the item list starts empty for them.

## Routes (admin only for now)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/context/{session_id}/items` | Load all items and revision |
| POST | `/context/{session_id}/items` | Add a decision or thread |
| POST | `/context/{session_id}/items/{item_id}/state` | Change an item's state |

All routes require the admin key (`X-Pi-Key`). Every write checks an `expected_revision` for
optimistic concurrency; a mismatch returns 409 `revision_conflict`.