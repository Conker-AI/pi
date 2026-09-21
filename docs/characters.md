# Owner-authored characters

`characters.py` persists the complete current dashboard Character Studio draft for
any existing agent, including Companion. This does not change agent creation,
archive, tool, memory, permission, or model authority. An archived agent's profile
and history remain readable; saves, restores, and imports reject archived agents.
No profile is invented for agents without one (`revision: 0`, `profile: null`).

Profiles include the authored name, profile line, personality, speaking style,
renderer preferences, portrait, emotion-to-art assignments, soul, backstory,
relationship, detail rows, preview examples, appearance assets/activity/expression
assignments, voice preferences/reference recording, and Focus/Character settings.
Field bounds and enums follow `dashboard/src/lib/api/character.ts`. Saves normalize
speakingPreset to custom, as the editor does. Requests use full expanded Studio
drafts; legacy omitted-studio frontend values must first pass through its draft
normalizer. Extra fields are rejected rather than becoming runtime authority.

## Persistence and API

Store initializes `characters.SCHEMA`. Head revisions point to immutable versions;
save and restore use `BEGIN IMMEDIATE` and expected_revision compare-and-swap.
Restore appends a new version containing the selected previous profile. Database
triggers reject rewriting, replacing, or deleting versions. Limits are 100 versions
and 128 MiB of serialized history per agent; reaching either returns 413. There is
no character-history deletion endpoint in this increment. Profiles are owner
configuration, not copied conversation history or synthesized memories.

Mount `characters_api.router(store_factory, authorize)` under the owner API:

- `GET /characters/{agent_id}?revision=N`: current or historical full profile.
- `PUT /characters/{agent_id}`: `{expected_revision, profile}`; initially revision 0.
- `GET /characters/{agent_id}/history`: revision/time/restore-source metadata.
- `GET /characters/{agent_id}/export?revision=N`: Conker v1 JSON package.
- `POST /characters/{agent_id}/restore`: `{expected_revision, revision}`.
- `POST /characters/{agent_id}/import`: `{text}` containing package JSON; returns a
  draft without saving it. A subsequent CAS save is required.

All routes depend on owner authorization. JSON responses are no-store and nosniff.
Mutation request streams are capped at 32 MiB before JSON parsing, including the
JSON wrapper. Stored serialized profiles and raw import strings have independent
32 MiB UTF-8 limits. Character errors expose static messages; validation errors do
not echo potentially large embedded data. Missing historical revisions return 404.

Conker v1 import validates the entire profile. Character Card V2/V3 imports map
only name, description, personality, scenario, mes_example, and first_mes; current
artwork/voice are retained. An existing full profile is required for card imports.
Card system prompts, extension packs, and other metadata do not create authority.
Import remains a draft and cannot silently replace an intervening revision.

## Inert media and truthful capability

Only embedded PNG/JPEG/WebP still images (2 MiB each), MP4/WebM video (8 MiB), and
WAV/MP3/OGG/WebM/MP4 audio references (8 MiB) are accepted, plus the fixed frontend
`/conker.png` image reference. No arbitrary path or external URL is accepted or
fetched. Base64 is validated, bounded, and checked against supported signatures;
PNG chunks/checksums and WebP container lengths receive additional validation.
Animated PNG/WebP are rejected. These are container/signature checks, **not full
codec decoding or a guarantee of browser playback**. Content remains inert JSON;
this module neither serves executable media nor invokes players or processors.

Voice engine `qwen3-tts`, language, design, reference, transcript, pronunciation,
per-mode delivery, expressiveness, and motion are persisted preferences. They do
not establish a connected synthesis engine, cloning, emotion inference, animation,
or perception. Authored preview dialogue is retained as examples, never reported
as a generated response. Actual adapter capabilities remain separate.

## Runtime boundary

`snapshot(db, agent_id)` returns `{revision, profile}` or None.
`runtime_snapshot(db, agent_id)` returns the same envelope with only name,
personality, speakingStyle and studio soul/backstory/relationship/details/modes/
voice metadata. Appearance, portrait, emotions, renderer and preview examples are
omitted. Voice reference becomes `{name, present: true}` without its data URI.
Callers freeze this reduced snapshot at request acceptance; later profile edits
affect future requests. Runtime mode comes from `presentationMode` (or the profile
default), with accepted call mode overriding conversation mode. Separate runtime
integration owns instruction selection; profile text never grants tools or memory.

The integrated Loop adds the authored text settings to actual model context.
Focus includes name and the owner's Focus text instructions; Character additionally
includes personality, speaking style, soul, backstory, relationship and details.
This controls new presentation instructions, not deletion of earlier conversation
history or guaranteed model compliance. Stored voice delivery preferences are not
silently converted into unsupported synthesis parameters.

Session settings accept optional `presentationMode`; null follows the profile's
default. Accepted turns freeze the reduced profile. Calls freeze it before STT,
and teams freeze each role's profile when the team run is created. Editing the
profile later does not rewrite accepted turns or team snapshots. Owner inspection
at `GET /sessions/{id}/settings/turns/{turn_id}` returns the saved execution
selection, including character revision and selected presentation mode. Model
credentials and embedded character assets are absent from that snapshot.

Validation: `pytest tests/test_characters.py -q` exercises restart persistence,
immutable versions, CAS races, restoration, archived/Companion boundaries, reduced
snapshots, package/media/history limits, rejected URLs/animated images/references,
import drafts, and owner API enforcement. Tests use temporary databases and
synthetic media only; no network, hardware, model download, or synthesis occurs.
