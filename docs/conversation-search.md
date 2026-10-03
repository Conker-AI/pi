# Conversation search

Owner-only GET `/search/conversations?query=...` searches retained user/assistant
text and returns exact session/message IDs, sequence, time, title and a bounded
excerpt around the match. `limit` is 1-50; `next_cursor` continues in stable
descending timestamp/ID order. Search characters `%`, `_` and backslash are literal.

The scope is explicitly `public-conversation-text`: forgotten conversations,
currently private conversations and conversations with historical private messages
are excluded. Tool payloads and structured nontext messages are excluded. Search
does not call a model, create embeddings or retain another transcript copy.
Matching uses Unicode casefold with literal substring matching; linguistic or
locale-aware ranking is not claimed.

This closes the conversation-text search backend gap. Cross-library search,
attachment text extraction and browser gateway wiring remain separate work.

## Layered owner search (development branch)

Owner-only GET `/search?q=...&stage=metadata|text|semantic` projects conversations,
projects, artifacts, tasks, jobs, authored agents, tool inventory, activity and
MemoryGate library records. Each result carries a stable identity, actual source
link, bounded excerpt, match type and stage. Pages contain at most 50 results.
Revision-bound cursors reject changed queries/settings. Sources report searched,
partial, unavailable or degraded coverage; dependency errors expose no raw data.

GET `/search/settings` and `/search/capabilities`, and verified owner POST
`/search/settings`, control included sources and optional stages. Saves require
`expected_revision`. Exact search needs no provider; semantic retrieval and AI
ranking default off and unsupported enablement is rejected. These routes are not
runtime or session-only write permissions.

Only MemoryGate provides semantic retrieval. Lexical fallback is labeled literal,
not semantic. Other sources scan at most 200 records. Artifact exact search covers
the latest markdown/code body, not every format or historical version; Memory
exact search covers library previews. Both report partial text coverage. No new
cross-source embedding index or rebuild controls exist yet. Source-owned reads
recheck forgotten/private origins; no secondary retained content index is stored.

Optional `search-ranking` ranks at most eight permitted previews within each
literal page. Invalid rankings and provider failures retain the literal order.
It cannot execute tools, send messages, change permissions or synthesize matches.
Activity excludes raw payloads, jobs exclude targets/arguments, and tools expose
inventory metadata only. Existing record APIs recheck access when results open.
