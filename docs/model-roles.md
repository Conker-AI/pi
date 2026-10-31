# Model catalogue and roles

The backend catalogue stores stable owner model IDs, provider binding IDs and actual provider routes. Answer, routing, context-selection and summarization each have explicit eligibility, enabled primary model, transport timeout and stop/fallback behavior. All configured models/providers must exist; manual answer locks cannot configure fallback.

With `failure: "fallback"` a role lists up to 8 backup models in `fallbackModelIds`, tried in that order after its own model fails; the first that answers wins, and none of them is a substitute the owner did not list. Each backup is eligible, enabled, listed once and not the role's own model; `stop` lists none. When the routing helper fails, the answer starts with the first backup. Settings saved with one `fallbackModelId` read as a list of one, and clients may still send it: it replaces the list, as an older client means.

`model_roles.dispatch` uses server-supplied adapters only. A catalogue entry cannot create a transport or retrieve credentials. An explicit answer override is a lock; unavailable locks stop. Automatic answer selection uses the configured routing role and validates its JSON `modelId` against current eligibility. A model such as Jev can be provided through an adapter; no vendor is hardcoded. Helper roles are disabled by no-harness; owner overrides still work. Attempts report requested and actual model separately.

Existing Ollama and OpenRouter adapters now expose `complete_bounded` for request-specific timeout, without mutating shared adapter settings. HTTP inactivity timeout does not guarantee cancellation of remote computation or total wall-clock duration. Credentials and endpoint provisioning remain owned by host/provider setup; the catalogue contains neither API keys nor browser-supplied endpoints.

The owner-only `/models/configuration` API is mounted. Submission reservation freezes the catalogue and role configuration revision with the agent/session selections; turn binding retains it. Loop answer calls and summary helpers dispatch through that snapshot. Provider IDs resolve only against the existing server-owned local/hosted adapters by their adapter names. Unsupported provider bindings fail closed. No direct provider SDKs or browser-provided endpoints are added.

The answer role's selected model is authoritative. `defaultModelId` remains a catalogue default; it does not silently override an explicitly configured role. A selected agent `modelId` is an explicit answer override within that role's eligibility. Manual locks and agent overrides never invoke a routing helper or substitute another model after failure. No-harness permits manual hosted answers but blocks helper routing and summarization. Legacy deterministic routing remains only for snapshots without any saved role configuration, including turns reserved before the first catalogue save.

Successful configured answers record `route_tier= configured`, `route_reason=configured`, and JSON attempt evidence in the turn detail (configuration revision, role, selected stable ID, requested route and actual returned model). This evidence is not a claim that final-completion token/cost fields aggregate all helper calls. Snapshot lookup verifies the turn's session or submission's original session before returning data. Frontend gateway wiring remains deferred.

Validation:

The workflow builder uses the optional `drafting` role when it is enabled with a model, and
the answer role otherwise, so chat can stay on a small local model while drafts use a stronger
one. Older saved settings gain it disabled. It runs only when the owner asks for a draft, with
the builder's 10-minute wait, and sees only the request and the capability list.

The `reviewer` role checks an action before it runs without the owner (AI decides, and whether
his own words ask for exactly this; see access.md). When it is enabled with a model, the reviewer
uses it, so he can make it stronger than the model doing the work (for example Jev); otherwise,
and in a chat with no harness, the answer model reviews. Older saved settings gain it disabled.
A reviewer that fails or answers unclearly means the action asks him.

`memory-sorting` sorts what MemoryGate hears into memory candidates, and `tool-text` writes the
text a ToolGate tool step asks a model for. Those services call them through `POST /inference`
(see inference.md). Until the owner picks a model for one, or while it is switched off, it uses
the local model, so nothing leaves the server; older saved settings gain them disabled with a
300-second wait.

A role may wait up to 600,000 ms (10 minutes) for its model. Saved settings keep their wait; when
nothing was saved, a role whose model is local gets 300,000 ms (a cold local model can take
minutes) and the others keep their old defaults.

The development search API has a separate optional `search-ranking` role,
migrated disabled with a 2,000ms transport timeout. It does not reuse memory
ranking or conversation context selection. Owner search settings must explicitly
enable it after a valid enabled model role is configured. It receives a bounded
set of permitted excerpts; invalid output falls back to literal results. These
settings do not enable autonomous work or alter conversation privacy.

```sh
python -m pytest tests/test_model_role_execution.py tests/test_model_roles.py tests/test_session_settings.py tests/test_loop.py tests/test_submissions.py tests/test_tool_turns.py -q
```
