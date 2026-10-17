# Answer judging and votes

The blind judge scores answer versions without knowing which model wrote each answer. Each version is labeled with a letter (A, B, C...) that gets shuffled, so the judge sees only the content without any model names or identifiers. The judge never scores answers written by its own model ; those answers are left out and listed in `excluded`. An answer or question with the `exclude` context rule, or one that was redacted or forgotten, is never sent to a judge.

Judging uses strict fail-closed parsing. Any malformed response from the judge model results in a 503 error with nothing stored. The judge must return exactly the expected JSON structure with scores 1-10 and short reasons, and the winner must be the highest-scoring answer.

Judge requests are idempotent by request ID. A replay with the same request ID returns the stored result without calling any model again, but the same ID with different data conflicts. Each new judgement costs one helper model call; a replay costs nothing. Neither judging nor votes select a version, approve anything or change an answer: they are data that helps the owner choose.

The judge routes:
- `POST /sessions/{sid}/response-families/{root}/judge` - Run a blind judge
- `GET /sessions/{sid}/response-families/{root}/judgements` - List past judgements

The owner vote routes:
- `POST /sessions/{sid}/response-families/{root}/vote` - Vote up, down, winner, or clear
- `GET /sessions/{sid}/response-families/{root}/votes` - Get current votes

Where scores are summarised (model profiles), owner votes outweigh the judge. Only one "winner" vote is allowed per family - a second winner automatically turns the first into an "up" vote.
