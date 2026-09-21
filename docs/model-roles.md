# Model catalogue and roles

The backend catalogue stores stable owner model IDs, provider binding IDs and actual provider routes. Answer, routing, context-selection and summarization each have explicit eligibility, enabled primary model, transport timeout and stop/fallback behavior. All configured models/providers must exist; manual answer locks cannot configure fallback.

`model_roles.dispatch` uses server-supplied adapters only. A catalogue entry cannot create a transport or retrieve credentials. An explicit answer override is a lock; unavailable locks stop. Automatic answer selection uses the configured routing role and validates its JSON `modelId` against current eligibility. A model such as Jev can be provided through an adapter; no vendor is hardcoded. Helper roles are disabled by no-harness; owner overrides still work. Attempts report requested and actual model separately.

Existing Ollama and OpenRouter adapters now expose `complete_bounded` for request-specific timeout, without mutating shared adapter settings. HTTP inactivity timeout does not guarantee cancellation of remote computation or total wall-clock duration. Credentials and endpoint provisioning remain owned by host/provider setup; the catalogue contains neither API keys nor browser-supplied endpoints.

The owner-only `/models/configuration` API is mounted. Submission reservation freezes the catalogue and role configuration revision with the agent/session selections; turn binding retains it. Loop answer calls and summary helpers dispatch through that snapshot. Provider IDs resolve only against the existing server-owned local/hosted adapters by their adapter names. Unsupported provider bindings fail closed. No direct provider SDKs or browser-provided endpoints are added.

The answer role's selected model is authoritative. `defaultModelId` remains a catalogue default; it does not silently override an explicitly configured role. A selected agent `modelId` is an explicit answer override within that role's eligibility. Manual locks and agent overrides never invoke a routing helper or substitute another model after failure. No-harness permits manual hosted answers but blocks helper routing and summarization. Legacy deterministic routing remains only for snapshots without any saved role configuration, including turns reserved before the first catalogue save.

Successful configured answers record `route_tier= configured`, `route_reason=configured`, and JSON attempt evidence in the turn detail (configuration revision, role, selected stable ID, requested route and actual returned model). This evidence is not a claim that final-completion token/cost fields aggregate all helper calls. Snapshot lookup verifies the turn's session or submission's original session before returning data. Frontend gateway wiring remains deferred.

Validation:

```sh
python -m pytest tests/test_model_role_execution.py tests/test_model_roles.py tests/test_session_settings.py tests/test_loop.py tests/test_submissions.py tests/test_tool_turns.py -q
```
