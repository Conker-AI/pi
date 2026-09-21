# Conversation search

Owner-only GET `/search/conversations?query=...` searches retained user/assistant
text and returns exact session/message IDs, sequence, time, title and a bounded
excerpt around the match. `limit` is 1-50; `next_cursor` continues in stable
descending timestamp/ID order. Search characters `%`, `_` and backslash are literal.

The scope is explicitly `public-conversation-text`: forgotten conversations,
currently private conversations and conversations with historical private messages
are excluded. Tool payloads and structured nontext messages are excluded. Search
does not call a model, create embeddings or retain another transcript copy.
SQLite's built-in text matching is case-insensitive for ASCII, with exact Unicode
substring matching; locale-aware ranking is not claimed.

This closes the conversation-text search backend gap. Cross-library search,
attachment text extraction and browser gateway wiring remain separate work.
