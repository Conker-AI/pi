# Model catalogue and roles

The backend catalogue stores stable owner model IDs, provider binding IDs and actual provider routes. Answer, routing, context-selection and summarization each have explicit eligibility, enabled primary model, transport timeout and stop/fallback behavior. All configured models/providers must exist; manual answer locks cannot configure fallback.

`model_roles.dispatch` uses server-supplied adapters only. A catalogue entry cannot create a transport or retrieve credentials. An explicit answer override is a lock; unavailable locks stop. Automatic answer selection uses the configured routing role and validates its JSON `modelId` against current eligibility. A model such as Jev can be provided through an adapter; no vendor is hardcoded. Helper roles are disabled by no-harness; owner overrides still work. Attempts report requested and actual model separately.

Existing Ollama and OpenRouter adapters now expose `complete_bounded` for request-specific timeout, without mutating shared adapter settings. HTTP inactivity timeout does not guarantee cancellation of remote computation or total wall-clock duration. Credentials and endpoint provisioning remain owned by host/provider setup; the catalogue contains neither API keys nor browser-supplied endpoints.

The owner-only router is ready for Store/API mounting. Per-turn configuration capture and Loop role dispatch integration are still required. Frontend gateway wiring remains deferred. Tests cover strict configuration, revision persistence, manual lock/no substitution, server-filtered eligibility, replaceable router choice, malformed decisions, explicit fallback and privacy. Model roles plus existing model readiness/audit/routing suites: 51 passed.
