# Bounded English speech adapters

`pi.speech.SpeechClient` provides synchronous, OpenAI-compatible HTTP transcription
and synthesis. It does not capture a microphone, play audio, persist audio, install
models, launch servers or retry failed requests. Call lifecycle/idempotency and
playback belong to the separate call runtime and client.

## Server choice and evidence

Primary documentation reviewed on 2026-09-21:

- [Speaches installation](https://speaches.ai/installation/) documents a CPU image
  and CPU Compose configuration. Its [introduction](https://speaches.ai/) describes
  faster-whisper STT and Piper/Kokoro TTS. This is the practical CPU-first server
  option for this adapter; no particular local latency or voice quality is claimed.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) documents CPU INT8
  inference and segment timestamps. Speaches documents the compatible
  [transcription endpoint](https://speaches.ai/usage/speech-to-text/) and
  [synthesis endpoint with WAV output](https://speaches.ai/usage/text-to-speech/).
  Model IDs and voices must match the server's installed catalogue.
- [vLLM-Omni's speech API](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/)
  documents `/v1/audio/speech` support, including Qwen3-TTS. The
  [Qwen3-TTS project](https://github.com/QwenLM/Qwen3-TTS) describes English support,
  preset voices and model-specific capabilities. Qwen3-TTS can be used behind an
  explicitly configured compatible server; this transport does not prove CPU
  performance, emotion control, voice cloning or real-time streaming support.

No server is selected, installed, downloaded, contacted or activated by importing
the adapter. Empty configuration performs no network calls. A local API base such
as `http://127.0.0.1:<configured-port>/v1` is an optional operator choice; there is
no default cloud endpoint or implicit provider credential.

## Python contract

```python
client = SpeechClient(
    url="http://127.0.0.1:8000/v1",  # example only; server must be separately configured
    key="",                       # optional bearer key for an authenticated server
    stt_model="installed-english-stt-model",
    tts_model="installed-tts-model",
    voice="installed-english-voice",
    timeout=30,
)
transcript = client.transcribe(audio_bytes, "audio/wav")
generated = client.synthesize("The text the owner asked to hear.")
```

`transcribe` sends multipart `file`, configured `model`, `language=en`,
`response_format=verbose_json`, and `timestamp_granularities[]=segment` to
`{url}/audio/transcriptions`. It returns:

```text
{text: str, segments: [{start: seconds, end: seconds, text: str}] | None,
 duration_seconds: float, language: str | None}
```

Duration is measured from the supplied PCM frames. Language is only the server's
reported field; requesting English does not establish independent language
detection. Silence can yield an empty transcript. Missing segments remain `None`;
the client does not estimate timing or manufacture word alignments. Returned segment
times must be finite, ordered without overlap, and within measured audio duration.

`synthesize` sends configured `model`, `voice`, `input` and `response_format=wav` to
`{url}/audio/speech`. It returns `{audio: bytes, mime: "audio/wav", duration_seconds}`.
Both response MIME and actual WAV structure are validated. No raw audio is written
to disk. Callers decide how long in-memory bytes remain available and must discard
late results after interruption. This synchronous transport has no cancellation API;
the caller must check its generation/consent guard before and after each invocation.

`SpeechError` has `.code`, `.status`, `.detail={code,message}` and a static message.
Provider response bodies, URL credentials and underlying transport error strings are
never copied into it. `capabilities()` is read-only and does not probe a server:
STT/TTS statuses are `unconfigured`, `configured` (not yet verified), `available`
(latest operation succeeded), or `unavailable` (latest operation failed). It is not
a continuously refreshed health check. Emotion controls, word timestamps and
streaming are explicitly unsupported by this adapter.

## Character voice design

`SpeechClient(..., character_voice="qwen3-design")` explicitly opts the configured
server into the documented vLLM-Omni Qwen3 VoiceDesign contract. The default is
`unsupported`; selecting a Qwen model name alone does not enable extensions.
The operator must separately serve a compatible VoiceDesign model and configure
its model ID. This implementation does not install or launch it.

`synthesize(text, presentation=accepted_call_preferences)` uses the character
snapshot frozen at request acceptance. Voice description, pronunciation guidance,
selected mode's voice notes and expressiveness preference become `instructions`;
the request selects `task_type=VoiceDesign`, `language=English` and WAV output.
No preset `voice` is sent for design. Focus adds restrained delivery guidance;
both modes retain the authored identity description. Personality/backstory,
artwork and reference audio do not enter the synthesis request.

This is instruction transport, not a calibrated expressiveness control, emotion
inference or a guarantee of consistent voice identity. `/calls/capabilities`
reports these limits under `speech.character_voice`. Empty designs, non-English
languages and reference-source voices fail explicitly before HTTP. Reference
cloning needs a separately supported model/adapter and remains unsupported here.
An ordinary request without a character still uses the configured default voice.
There is no fallback from an unsupported character voice to that default.

Calls retain text success and record a static speech error code if synthesis
fails. Existing interruption checks and once-only request receipts apply to
designed speech. No additional media persistence or server-side retention
guarantee is introduced. `tests/test_character_speech.py` verifies request payloads,
mode/identity freezing, future edits, explicit rejection, replay and interruption
using synthetic PCM, mock HTTP and temporary SQLite only.

## Boundaries

- PCM WAV only: RIFF/WAVE, integer PCM, one format/data chunk, mono/stereo,
  8000–192000 Hz, 8/16/24/32-bit samples; checked frame alignment, byte rate and
  container/chunk lengths. Accepted WAV MIME aliases are normalized to `audio/wav`.
  Compressed WAV, float WAV, MP3, Ogg and browser MediaRecorder WebM are rejected.
  Future browser capture needs a PCM encoder or a separately reviewed decoder.
- Audio input/output: at most 10 MiB and 120 measured seconds. TTS input: at most
  4000 nonblank characters. STT JSON: at most 256 KiB, 16000 transcript characters,
  and 1000 segments with 16000 total segment-text characters.
- HTTP redirects, environment proxies and compressed responses are disabled.
  Credential-bearing URLs, query/fragment URLs and malformed configuration fail
  closed. Bearer credentials are sent only to the explicitly configured endpoint.
- Connect/read/write/pool timeouts are bounded to the configured 0.1–120 seconds.
  A monotonic elapsed-time check rejects slow-drip responses between chunks; an
  individual blocked network operation remains subject to its HTTP timeout. This is
  a bounded synchronous transport, not a hard real-time cancellation guarantee.
- No fallback endpoint, automatic retry, timestamp inference, model download,
  microphone access, cloning reference audio or speech-quality claim is introduced.

Tests use synthetic PCM and `httpx.MockTransport` only:

```sh
python -m pytest tests/test_speech.py -q
python -m ruff check pi/speech.py tests/test_speech.py
```
