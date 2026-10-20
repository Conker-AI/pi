"""Call request and response models, and their browser projections."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from pi.core.models import Strict


class Channels(Strict):
    microphone: bool = False
    camera: bool = False
    keyboard: bool = True
    voice: bool = True
    avatar: bool = False
    captions: bool = True


class ChannelUpdate(Strict):
    microphone: bool | None = None
    camera: bool | None = None
    keyboard: bool | None = None
    voice: bool | None = None
    avatar: bool | None = None
    captions: bool | None = None


class Privacy(Strict):
    memory: bool = False
    harness: bool = False


class Start(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    conversationId: str = Field(min_length=1, max_length=200)
    privacy: Privacy = Field(default_factory=Privacy)


class Revision(Strict):
    expected_revision: int = Field(ge=1)


class Update(Revision):
    channels: ChannelUpdate | None = None
    mode: Literal["focus", "character"] | None = None
    paused: bool | None = None
    privacy: Privacy | None = None
    modelId: str | None = Field(default=None, min_length=1, max_length=200)


class Send(Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    language: Literal["en"] = "en"


class BrowserEventSource(Strict):
    sessionId: str = Field(min_length=1, max_length=200)
    messageId: str = Field(min_length=1, max_length=200)


class BrowserEvent(Strict):
    id: str = Field(min_length=1, max_length=200)
    at: float = Field(ge=0, allow_inf_nan=False)
    kind: Literal["event", "user", "assistant"]
    text: str = Field(max_length=16_000)
    source: BrowserEventSource | None = None


class BrowserRequest(Strict):
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    state: Literal[
        "reserved",
        "transcribing",
        "model",
        "synthesizing",
        "complete",
        "held",
        "failed",
        "unknown",
        "forgotten",
    ]
    inputKind: Literal["text", "audio"]
    speechStatus: str = Field(max_length=100)
    errorCode: str | None = Field(default=None, max_length=100)
    turnId: str | None = Field(default=None, max_length=200)
    turnStatus: str | None = Field(default=None, max_length=100)
    textStatus: Literal["complete", "not-complete"]
    acted: bool
    messageIds: list[str] = Field(max_length=4)


class BrowserCallCapabilities(Strict):
    typedTurns: Literal[True]
    speech: Literal["bounded-audio-turns"]
    camera: Literal["unavailable"]
    perception: Literal["unavailable"]
    emotion: Literal["unavailable"]
    characterVoice: Literal["adapter-dependent; inspect call capabilities"]
    channels: Literal["client-owned capture; preferences gate transport"]
    rawMediaRetention: Literal["none"]
    audioReplay: Literal["unavailable"]
    interruption: Literal["cooperative; in-flight effects cannot be recalled"]


class BrowserCall(Strict):
    schemaVersion: Literal[1] = 1
    id: str = Field(pattern=r"^call_[a-f0-9]{32}$")
    conversationId: str = Field(min_length=1, max_length=200)
    sessionId: str = Field(min_length=1, max_length=200)
    agentId: str = Field(pattern=r"^(?:companion|agent_[a-f0-9]{32})$")
    name: Literal["Call"]
    startedAt: float = Field(ge=0, allow_inf_nan=False)
    endedAt: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    phase: Literal["ready", "thinking", "responding", "ended"]
    paused: bool
    channels: Channels
    mode: Literal["focus", "character"]
    modelId: str | None = Field(default=None, max_length=200)
    privacy: Privacy
    events: list[BrowserEvent] = Field(max_length=200)
    requests: list[BrowserRequest] = Field(max_length=100)
    eventsTruncated: bool
    requestsTruncated: bool
    capabilities: BrowserCallCapabilities
    authority: Literal["none"] = "none"
    execution: Literal["typed-and-audio-turns"] = "typed-and-audio-turns"
    contentIncluded: bool
    audioIncluded: Literal[False] = False
    retention: Literal["transcript-persisted; raw-media-none"] = (
        "transcript-persisted; raw-media-none"
    )


class BrowserCallTurn(Strict):
    schemaVersion: Literal[1] = 1
    call: BrowserCall
    replayed: bool
    audioIncluded: Literal[False] = False
    transcriptionIncluded: Literal[False] = False
    execution: Literal["typed-turn"] = "typed-turn"


class BrowserAudio(Strict):
    base64: str = Field(min_length=1, max_length=14_000_000, pattern=r"^[A-Za-z0-9+/]+={0,2}$")
    mime: Literal["audio/wav"]
    durationSeconds: float | None = Field(default=None, ge=0, le=120, allow_inf_nan=False)
    retention: Literal["transient-response-only"]


class BrowserTranscription(Strict):
    text: str = Field(min_length=1, max_length=4000)
    durationSeconds: float | None = Field(default=None, ge=0, le=120, allow_inf_nan=False)
    timing: Literal["provider-words", "provider-segments", "unavailable"]
    retention: Literal["transient-response-only"]


class BrowserAudioTurn(Strict):
    schemaVersion: Literal[1] = 1
    call: BrowserCall
    replayed: bool
    audio: BrowserAudio | None = None
    transcription: BrowserTranscription | None = None
    audioIncluded: bool
    transcriptionIncluded: bool
    execution: Literal["audio-turn"] = "audio-turn"


class BrowserCallAvailability(Strict):
    schemaVersion: Literal[1] = 1
    typedTurns: Literal[True] = True
    language: Literal["en"] = "en"
    speechInput: Literal["configured", "available", "unavailable", "unconfigured"]
    speechOutput: Literal["configured", "available", "unavailable", "unconfigured"]
    inputMimeTypes: list[Literal["audio/wav"]] = Field(default_factory=lambda: ["audio/wav"])
    outputMimeTypes: list[Literal["audio/wav"]] = Field(default_factory=lambda: ["audio/wav"])
    maxAudioBytes: Literal[10485760] = 10485760
    maxAudioSeconds: Literal[120] = 120
    camera: Literal["unavailable"] = "unavailable"
    perception: Literal["unavailable"] = "unavailable"
    rawMediaRetention: Literal["none"] = "none"
    transcriptRetention: Literal["persisted-with-call-conversation"] = (
        "persisted-with-call-conversation"
    )
    devices: Literal["client-owned; not captured by Pi"] = "client-owned; not captured by Pi"
    authority: Literal["none"] = "none"


def browser_view(value):
    events, requests = value["events"][-200:], value["requests"][-100:]
    return BrowserCall(
        **{
            key: value[key]
            for key in (
                "id",
                "conversationId",
                "sessionId",
                "agentId",
                "name",
                "startedAt",
                "endedAt",
                "revision",
                "generation",
                "phase",
                "paused",
                "channels",
                "mode",
                "modelId",
                "privacy",
            )
        },
        events=events,
        requests=requests,
        eventsTruncated=len(events) != len(value["events"]),
        requestsTruncated=len(requests) != len(value["requests"]),
        capabilities={
            "typedTurns": True,
            "speech": "bounded-audio-turns",
            "camera": "unavailable",
            "perception": "unavailable",
            "emotion": "unavailable",
            "characterVoice": "adapter-dependent; inspect call capabilities",
            "channels": "client-owned capture; preferences gate transport",
            "rawMediaRetention": "none",
            "audioReplay": "unavailable",
            "interruption": "cooperative; in-flight effects cannot be recalled",
        },
        contentIncluded=bool(events),
    )


def browser_turn(value):
    return BrowserCallTurn(call=browser_view(value["call"]), replayed=value["replayed"])


def browser_audio_turn(value):
    transcription = value.get("transcription")
    if transcription is not None:
        transcription = {
            key: transcription.get(key)
            for key in ("text", "durationSeconds", "timing", "retention")
        }
    return BrowserAudioTurn(
        call=browser_view(value["call"]),
        replayed=value["replayed"],
        audio=value.get("audio"),
        transcription=transcription,
        audioIncluded=value.get("audio") is not None,
        transcriptionIncluded=transcription is not None,
    )


def browser_availability(speech=None):
    capabilities = speech.capabilities() if speech is not None else {}

    def status(name):
        value = capabilities.get(name, {})
        selected = value.get("status") if isinstance(value, dict) else value
        return (
            selected if selected in {"configured", "available", "unavailable"} else "unconfigured"
        )

    return BrowserCallAvailability(speechInput=status("stt"), speechOutput=status("tts"))
