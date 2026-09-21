# Durable call runtime

`pi.calls` implements owner-started English calls as durable children of existing
conversations. Typed input runs the actual Pi `Loop`; configured speech adapters
may run WAV transcription, the same Loop turn, then WAV synthesis. Tests use real
Loop orchestration with synthetic providers/audio. No hardware, cloud endpoint,
model download, browser gateway or frontend adapter is exercised or installed.

## Conversation and source boundary

Starting a call creates an ordinary child `sessions` row, leaving the main
conversation open and its transcript unchanged. Source history is represented by
`context_inherited_messages` references, preserving original message identities;
source context policy, exclusions, exact pins and instructions are retained.
Existing source summary text is read by reference, with a captured hash that
blocks future work if the summary changes. It is not copied into the child.
Automatic forks are blocked for calls so context and call identity cannot detach.

Call transcript events resolve real submission input/final message IDs. Event rows
store only static lifecycle labels, not another transcript. Source or child
forgetting makes the call unavailable. Parent forgetting includes the child via
the existing session tree; the call redaction hook clears input fingerprints,
source-summary fingerprints and stored preferences before the existing physical
SQLite cleanup. No separate call recording survives that operation.

Creation privacy conservatively combines source and inherited-origin exclusions.
Call memory/harness preferences may narrow access; they cannot relax prior
exclusions. The child's settings are separate from its parent's. The normal
settings endpoint cannot modify a call child behind these controls. Neither
source history nor presentation settings confer tool authority. Existing Pi and
ToolGate policies remain authoritative.

## Controls and model selection

Microphone, keyboard, voice, avatar and caption preferences are independent.
Microphone/camera start off; keyboard and voice start on. These are preferences,
not assertions that a device is capturing or a speaker is playing. Camera=true is
rejected: camera capture, video, perception and emotion inference are unavailable.
Focus/Character is a presentation preference and does not fabricate a personality,
an emotional state, a character voice, or a different reasoning service.

Pause, resume, interruption, privacy narrowing and input/output muting advance a
generation. Work accepted under an older generation cannot initiate a next model,
tool or audio stage. Muting microphone/voice conservatively interrupts the current
response. Ending advances the generation and prevents new work. Already in-flight
network operations cannot be recalled; the actual effect/turn outcome remains
visible, including `acted_no_reply` after an effect succeeds without a reply.

Model choice accepts an enabled configured model. Preferences are captured when
call input is accepted, so a model change during STT affects the next request.
Actual model eligibility, server provider availability and dispatch policy remain
in the existing model-role implementation. Main-conversation settings do not
change. In-flight selections never rewrite already captured turn settings.

## Idempotency, interruption and recovery

A globally unique request ID and exact input fingerprint commit before STT, model
or TTS. The same ID/input returns its durable receipt without another operation;
conflicting input is rejected. A call accepts only one active chain at a time.
The submission schema guard requires the exact reserved request for the call
child, preventing direct generic session submission from bypassing call state.

Execution snapshots carry `callExecution: {id, generation}`. Parent hooks invoke
`guard` before and after each model attempt (including routing/fallback), before
effect dispatch and before final completion. `guard_db` checks again inside the
final transcript write transaction, ordering a stop commit against answer commit.
A provider that returns after interruption cannot trigger a fallback, a next tool,
or an assistant-message append. Audio is checked again before response encoding.
This is cooperative cancellation, not remote request revocation or a claim that
already transmitted client audio can be recalled.

Startup `recover` marks unfinished call requests unknown, pauses open calls and
advances generations. It does not rerun STT/model/TTS or replay uncertain costs.
The existing turn-recovery procedure still records interrupted/acted outcomes.
Reconnection reads durable history and receipts; explicit resume permits new
request IDs. Unknown prior work is inspection-only.

Text success and speech success are distinct. TTS failure preserves the completed
assistant message and reports `textStatus: complete` with unavailable/failed
`speechStatus`. Generated audio is returned only in the original response; a
replayed request has no audio. No input/output media bytes are written to disk.
Input fingerprints are retained for idempotency until forgetting. Text transcripts
remain ordinary durable messages. Provider-side retention is outside this local
no-recording guarantee.

## Owner API and speech

Factory: `calls_api.router(store_factory, loop_factory, owner_authorize,
speech_factory=None)`.

- `POST /calls` with `request_id`, `conversationId`, optional narrowing `privacy`.
- `GET /calls?conversation_id=...` and `GET /calls/{id}` reconnect/inspect history.
- `GET /calls/capabilities` reports configured speech and unavailable perception.
- `POST /calls/{id}/update` uses `expected_revision` and preference fields.
- `POST /calls/{id}/interrupt` or `/end` uses `expected_revision`.
- `POST /calls/{id}/turns` uses `request_id`, `text` (1–4,000 characters), English.
- `POST /calls/{id}/audio?request_id=...` accepts bounded raw WAV bytes. Blocking
  speech/model work runs off the ASGI event loop so pause/end can arrive.

The `SpeechClient` interface is `transcribe(audio, mime)` returning text metadata
and `synthesize(text)` returning audio/mime/duration metadata. Calls guard both
sides of each invocation. The adapter owns WAV validation, duration/byte limits,
timeouts, credentials, configured capability reporting and no-retry behavior.
No speech adapter means typed text still works and voice is explicitly unavailable.
Pi configures the optional adapter with `PI_SPEECH_URL` (including the server's
API base path), `PI_SPEECH_KEY`, `PI_STT_MODEL`, `PI_TTS_MODEL`, and
`PI_TTS_VOICE`. `PI_SPEECH_TIMEOUT_S` defaults to 30 seconds. There is no default
speech host or downloaded model. Configuration is validated before opening Pi's
database; capability reporting distinguishes configuration from actual success.
Speech response audio is base64 in a transient JSON envelope, not a persisted file
or a browser playback claim. Precise playback/word alignment, live streaming,
client devices, echo suppression, reconnect UI, incoming calls and cross-device
handoff remain outside this increment.

## Verification

Fifteen focused tests cover actual typed Loop turns, exact-source history and
exclusions, isolated privacy, replay/restart, real provider interruption, stopped
model fallback, atomic final-commit cancellation, in-flight tool outcome reporting,
STT/Loop/TTS-once behavior, failed/interrupted synthesis with retained text,
future-only model changes, parent forgetting/byte erasure and owner API smoke.
Scoped Ruff checks pass. Parent integration tests separately cover its shared
hooks and generic child-settings protection; this is not a live device/service
validation or a declaration that every P13 frontend capability is connected.
