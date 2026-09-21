# Specialist memory read authority

`PI_MEMORY_AGENT_READ_BINDINGS` is an operator-owned JSON object mapping Pi agent
IDs to `{ "namespace": "memorygate-namespace", "keyEnv": "READ_KEY_ENV_NAME" }`.
The named environment variable contains an owner-provisioned MemoryGate read key
authorized for that namespace. Agent prompts/configuration cannot choose this
mapping or credential. MemoryGate's existing authorization remains authoritative.

The existing MemoryGate URL/configuration must be enabled first. Bindings are
validated at startup and invalid/missing keys fail startup with a static error.
Companion retains its existing client; unbound specialists receive no memory and
never fall back to Companion. Selected/conversation scopes still apply within the
bound namespace, and memory-disabled privacy prevents reads. Specialist clients
have no ingestion credential and do not participate in Companion's delivery worker.
No keys or namespaces were provisioned by this change.

Team-role reads use the assigned agent's binding and the role's existing narrowed
memory selection. Conversation scope uses the original owner conversation, not a
new team-step session. Read privacy is frozen from that source at team preparation;
current source privacy is checked again before retrieval, so later disabling or
forgetting the source blocks reads. Team-step transcripts retain memory-disabled
flags and are not ingested. Older prepared teams without read policy stay disabled.
This does not add specialist ingestion or wire the UI.
Fourteen focused authority and existing memory tests passed using synthetic clients
and temporary stores; no real MemoryGate deployment was contacted.
