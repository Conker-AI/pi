# Saved helper-model evaluations

This package stores owner-authored cases for `routing`, `context-selection`, and
`summarization`, and evaluates them through the existing `model_roles.dispatch`.
There is no hardcoded vendor, fabricated answer, automatic promotion of defaults,
or implicit import of conversations/memories. Providers are injected server-owned
adapters; only an explicit authorized POST starts an evaluation. Tests use stubs
only and make no external calls.

## Case contract

A case has `name` (1–80 characters), `role`, and `prompt` (1–16000 characters).

- Routing: `candidateIds` contains enabled catalogue model IDs; `expectedIds`
  contains exactly one candidate. The model must return only `{"modelId":"..."}`.
- Context selection: candidate and expected IDs identify owner-supplied messages
  in the case prompt. The model must return only `{"messageIds":["..."]}`.
  An empty expected set is permitted. No session content is fetched automatically.
- Summarization: `requiredFacts` contains 1–20 distinct phrases, each up to 500
  characters. ID lists must be empty.

ID lists have at most 100 distinct IDs, each at most 200 characters. Types are
strict, unknown fields are rejected, and expected IDs must belong to candidates.
Expected IDs and required facts are scoring labels; they are not sent to the model.
Names are unique across active and archived cases. Changes require revision checks
and preserve immutable historical definitions. Archive/restore increments the case
revision; archived cases cannot be edited or evaluated. There is no hard deletion.

## API and mounting

Mount `model_evaluations_api.router(store, authorize, providers)` where `store()`
returns the existing Pi Store, `authorize` is the admin-only dependency, and
`providers()` returns the existing server adapter map keyed by configured provider
ID. The factory never accepts transports, endpoint URLs or credentials from clients.
Gateway/frontend wiring is intentionally separate.

| Method | Path below `/model-evaluations` | Input |
|---|---|---|
| GET / POST | `/cases` | POST takes the case definition |
| GET | `/cases/{id}` | Current definition and revision |
| POST | `/cases/{id}/update` | `{expected_revision, definition}` |
| POST | `/cases/{id}/archive` | `{expected_revision, archived}` |
| POST | `/cases/{id}/runs` | `{request_id, expected_case_revision, expected_configuration_revision}` |
| GET | `/runs` | Optional `case_id` and bounded `limit` |
| GET | `/runs/{request_id}` | Durable run receipt |

Integration initializes `model_evaluations.SCHEMA` in Store after model-role tables.
Once at runtime startup, before evaluation requests can execute,
`model_evaluations.recover_interrupted(store)` marks outstanding receipts. Do not
call recovery while evaluators are active. A late returning caller cannot overwrite
an interrupted receipt: terminal writes compare-and-set only a still-running row.

## Execution and recovery

Reservation atomically captures the case revision and entire model configuration
revision before calling a provider. The chosen role follows its configured primary,
explicit eligibility, timeout and fallback policy. The immutable run snapshot is
not changed by later case or catalogue edits.

The request ID is an idempotency identity. Identical retries return the existing
receipt even if a provider call is still running, the case was archived, or settings
changed. Reusing the ID with different expected revisions/case returns 409. A restart
marks unfinished runs `interrupted`, with outcome/usage unknown; neither recovery nor
retry repeats a model call. After review, an owner can explicitly submit a new ID.
This cannot guarantee a remote server stopped work after a timeout or lost response.

## Evidence and metric limits

Results retain successful dispatcher attempt metadata and a separate list of actual
provider calls, including requested route, returned model, per-call latency and
reported token/cost usage. Failed calls retain unknown usage and no arbitrary raw
exception text. Missing adapters may appear only in dispatcher metadata; if dispatch
fails before returning metadata, `providerCalls` still records calls actually made.
Aggregate usage is null when any call lacks that field, rather than treating unknown
cost as zero. Overall latency covers dispatch and scoring, not queue wait or storage.

Routing/context scores require strict JSON and exact expected ID sets. Duplicate
keys, duplicate IDs, extra fields and out-of-candidate selections fail validation.
Summary score is **case-insensitive literal phrase coverage**, not an entailment or
factuality judgment: paraphrases may score poorly and contradictory text may still
contain every phrase. All results declare `semanticCorrectnessVerified: false`.
No score establishes provider capability, safety or semantic correctness. Outputs
over 32000 characters receive a failed bounded-output metric and are not stored;
actual reported usage remains recorded. No score changes configuration automatically.

Validation (temporary SQLite databases, no live providers):

```sh
python -m pytest tests/test_model_evaluations.py tests/test_model_roles.py -q
git diff --check
```
