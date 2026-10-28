# Owner settings

Conker's settings registry (in the Conker repository, one file per topic) describes every setting
once: its title, type, default and home. Settings with a home of their own stay there: quiet hours,
proactivity, daily budgets, plan alerts and the idle timeout in owner preferences
(`/owner/preferences`), models in `/models/configuration`, search in `/search/settings`, chat
settings in each chat. Every other setting lives in the owner's settings document, keyed by its
registry id.

- `GET /owner/settings` returns `{schemaVersion, revision, values}`.
- `POST /owner/settings` with `{expected_revision, values}` replaces the document (a value left out
  goes back to its default). A stale revision gets 409 `revision_conflict` with `current_revision`.
- Ids look like `group.name` (lowercase, 2-7 dotted parts); values are plain JSON, at most 8 KiB
  each, 4 levels deep and 2,000 settings in all. Anything else gets 422 `invalid_settings`.

Both need the owner credential (the Settings screen) or the recovery key (`conker config`). Saving
needs only the signed-in browser session: this document holds preferences, never authority. What
Conker may do lives in ToolGate and in documents that keep asking for the password. Owner
preferences moved from the recovery key to the owner credential so the Settings screen can show
them; saving them still asks for the password at the gateway.

A feature that reads a value here (`pi.owner.settings.value`) checks it again itself: this store
knows each value's shape, not its meaning.
