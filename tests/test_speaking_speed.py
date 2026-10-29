"""The owner's speaking speed reaches the speech server; the normal speed sends nothing new."""

import json

import httpx
import pytest
from test_calls import Provider, send, start
from test_speech import client, wav

from pi.calls import speech
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.owner import settings as owner_settings
from pi.turns.loop import Loop


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "calls.db")
    yield value
    value.close()


def choose(store, value):
    current = owner_settings.load(store)
    owner_settings.save(
        store,
        owner_settings.Update(expected_revision=current["revision"], values={speech.SPEED: value}),
    )


@pytest.mark.parametrize(
    ("chosen", "speed"),
    [
        (None, None),
        (100, None),
        (150, 1.5),
        (50, 0.5),
        (200, 2.0),
        (10, 0.5),
        (900, 2.0),
        (125.5, 1.255),
        ("fast", None),
        (True, None),
        ([150], None),
    ],
)
def test_the_owners_speed_is_checked_and_kept_to_half_to_double(store, chosen, speed):
    if chosen is not None:
        choose(store, chosen)
    assert speech.owner_speed(store) == speed


@pytest.mark.parametrize(("speed", "sent"), [(1.5, 1.5), (0.5, 0.5), (1, None), (None, None)])
def test_synthesis_sends_the_speed_only_when_it_is_not_the_normal_one(speed, sent):
    audio = wav(0.5)
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    client(handle).synthesize("Hello", speed=speed)
    expected = {"model": "tts-model", "voice": "english-voice", "input": "Hello"}
    assert bodies == [
        {**expected, "response_format": "wav", **({} if sent is None else {"speed": sent})}
    ]


@pytest.mark.parametrize("speed", [0.4, 2.5, float("nan"), float("inf"), "1.5", True])
def test_a_speed_outside_half_to_double_is_refused_before_any_request(speed):
    def forbidden(request):
        pytest.fail("A refused speed must not reach the speech server")

    with pytest.raises(speech.SpeechError) as refused:
        client(forbidden).synthesize("Hello", speed=speed)
    assert refused.value.code == "invalid_speech_speed"


class Speech:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"stt": {"status": "configured"}, "tts": {"status": "configured"}}

    def synthesize(self, text, **options):
        self.calls.append(options)
        return {"audio": b"transient audio", "mime": "audio/wav", "duration_seconds": 1}


def test_a_spoken_answer_uses_the_owners_speed_and_none_at_the_normal_one(store):
    voice = Speech()
    loop = Loop(store, Router(local_provider=Provider(), local_model="fake"))
    call = start(store)
    send(store, loop, call, 1, speech=voice)
    choose(store, 140)
    send(store, loop, call, 2, speech=voice)
    assert voice.calls == [{}, {"speed": 1.4}]
