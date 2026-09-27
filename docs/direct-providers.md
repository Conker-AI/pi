# Direct text providers

September 21, 2026. The accepted Settings catalogue offers OpenRouter, Anthropic
and OpenAI. Ollama and OpenRouter reuse existing adapters. Direct OpenAI Chat
Completions and Anthropic Messages now implement Pi's `complete_bounded` seam.

Server setup supplies `PI_OPENAI_KEY_FILE` and/or `PI_ANTHROPIC_KEY_FILE` as
absolute, regular, read-only files. `PI_OPENROUTER_KEY_FILE` provides the same
boundary for OpenRouter. Pi rejects a provider configured through both a direct
environment value and a file, reads at most 4096 bytes once during startup, and
never returns the value. The direct environment names remain compatibility inputs
for standalone Pi deployments, not Conker's deployment contract. These adapters
refuse requests unless `PI_ALLOW_PAID_MODELS` is `1`, `true` or `yes`, matching
the existing hosted opt-in. `PI_HOSTED_TIMEOUT_S` supplies the default ceiling;
configured roles pass their own existing timeout. No keys are loaded from a
Settings draft or persisted in the model configuration. Restart after setup.

Use provider IDs `openai` or `anthropic` in the existing model-role configuration
and supply the provider's model ID as `route`. Explicit roles, context helpers,
and model evaluations use the same registered adapters. Existing eligibility,
manual locks, helper privacy, frozen execution settings, and team metering still
apply. Legacy free-model escalation remains Ollama/OpenRouter only; registering
a direct key does not make paid direct models automatic fallback candidates.

`GET /models` includes `direct` entries for configured keys. Each reports text
capability and manual discovery. Health checks do not issue paid test requests:
configured authority is `unverified`, missing spending authority is
`not_configured`. Account access and model availability require later authorized
host verification. `/health` separately reports the configured provider identities
with `verification: host-revision-bound` and `secrets_included: false`; it does not
receive or expose host revision metadata. Prices are not guessed: completion cost
remains unknown.

The adapters accept text system/user/assistant messages. Anthropic lifts leading
system messages into its top-level system field and rejects late system messages
rather than changing their order. It uses a 4096-token output ceiling. Token
receipts include cached tokens; Anthropic input totals include its separate
uncached/cache-read/cache-creation counters. Error bodies and exception text are
not returned. Requests do not follow redirects or retry automatically.

The Conker browser shows configured readiness but does not accept credentials or
endpoints. This module does not implement dynamic
direct-model discovery, multimodal/native-tool payloads, reasoning-effort controls,
streaming, or direct-provider citation translation. Pi's existing text tool
protocol remains separate from provider-native tool calls. Frontend wiring and
provider-native account management.

Protocol references:
[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Anthropic Messages](https://platform.claude.com/docs/en/api/typescript/messages).

Verification uses synthetic HTTP responses and temporary SQLite databases only.
`tests/test_direct_providers.py` covers translation, usage, timeout propagation,
spending guards, malformed/error responses, registry dispatch and locked failure.
Existing role tests cover no-harness helper exclusion and manual-lock behavior.
