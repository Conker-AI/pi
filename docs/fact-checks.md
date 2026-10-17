# Fact checks

A fact check evaluates an answer's checkable claims against only the sources the answer
actually used: the `research.web` search results, `research.fetch` page reads, and saved
citations from that turn. It does not search again; web re-checking is a separate step.

Each claim is marked **supported** (a source states it), **contradicted** (a source states
otherwise), or **no-source** (no source covers it). Results are strict: the model's
response is parsed against an exact schema and any deviation fails closed, storing nothing.

Request ids are idempotent: replaying the same id returns the stored result without calling
a model. A different session, message or body with the same id is rejected as a conflict.

Fact checks are refused when the answer has no recorded sources (the check has nothing to
compare against) or when helper models are off for the chat. Results are data: they never
change, approve or spend anything about the answer they checked.
