import base64
import io
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from pi import (
    attachment_turns,
    attachments,
    context_controls,
    image_input,
    session_settings,
    turn_context,
)
from pi.direct_providers import AnthropicProvider, OpenAIProvider
from pi.loop import Loop
from pi.openrouter import OpenRouterProvider
from pi.providers import Completion, Message, OllamaProvider, ProviderUnavailable, require_images
from pi.routing import Router
from pi.store import Store


def picture(format="PNG", size=(8, 8)):
    output = io.BytesIO()
    Image.new("RGB", size, "red").save(output, format=format)
    return output.getvalue()


@pytest.mark.parametrize(
    "format,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_image_validation_preserves_exact_original_bytes(format, media):
    raw = picture(format)
    value = image_input.prepare(raw, media, "attachment_a")
    assert base64.b64decode(value.data) == raw
    assert value.data not in repr(value)


@pytest.mark.parametrize("case", ["wrong-media", "broken", "oversized", "svg"])
def test_invalid_images_refused(case):
    raw, media = picture(), "image/png"
    if case == "wrong-media":
        media = "image/jpeg"
    elif case == "broken":
        raw = b"not an image"
    elif case == "oversized":
        raw = picture(size=(3000, 2000))
    else:
        raw, media = b"<svg/>", "image/svg+xml"
    with pytest.raises(ValueError, match="valid still"):
        image_input.prepare(raw, media, "attachment_a")


@pytest.mark.parametrize("kind", ["openai", "anthropic", "openrouter", "ollama"])
def test_real_adapters_transmit_image_in_provider_format(monkeypatch, kind):
    image = image_input.prepare(picture(), "image/png", "attachment_a")
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        body = (
            {"content": [{"type": "text", "text": "red"}]}
            if kind == "anthropic"
            else {"message": {"content": "red"}}
            if kind == "ollama"
            else {"choices": [{"message": {"content": "red"}}]}
        )
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    adapter = {
        "openai": lambda: OpenAIProvider("synthetic", allow_paid=True),
        "anthropic": lambda: AnthropicProvider("synthetic", allow_paid=True),
        "openrouter": lambda: OpenRouterProvider("synthetic"),
        "ollama": lambda: OllamaProvider("http://synthetic"),
    }[kind]()
    if kind == "openrouter":
        monkeypatch.setattr(
            adapter, "_guard_cost", lambda model: SimpleNamespace(supports_images=True)
        )
    assert (
        adapter.complete([Message("user", "Describe", (image,))], model="synthetic").text == "red"
    )
    message = sent[0]["messages"][0]
    if kind == "ollama":
        assert message["images"] == [image.data]
    elif kind == "anthropic":
        assert message["content"][0]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": image.data,
        }
    else:
        assert message["content"][1]["image_url"]["url"] == "data:image/png;base64," + image.data


def test_unknown_adapter_cannot_silently_drop_images():
    image = image_input.prepare(picture(), "image/png", "attachment_a")
    with pytest.raises(ProviderUnavailable, match="cannot receive images"):
        require_images(object(), [Message("user", "See", (image,))])


def test_image_turn_and_original_context_replay_preserve_image(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid, other = store.create_session(), store.create_session()
        raw = picture()
        item = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="image.png", type="image/png"),
            raw,
            resolve=session_settings.source_privacy,
        )
        calls = []

        class Provider:
            name = "local"
            supports_images = True

            def complete(self, messages, *, model):
                calls.append(messages)
                return Completion(provider=self.name, model=model, text="Red image")

        loop = Loop(store, Router(local_provider=Provider(), local_model="vision"))
        result = loop.run_turn(
            sid, "Describe", request_id="image_request_01", attachment_ids=[item["id"]]
        )
        images = [image for message in calls[0] for image in message.images]
        assert len(images) == 1 and base64.b64decode(images[0].data) == raw
        assert images[0].attachment_id == item["id"]
        with closing(Store(store.path)) as reopened:
            replay = turn_context.replay(reopened, result["turn_id"])
            assert [image for message in replay["messages"] for image in message.images] == images
            with pytest.raises(attachments.AttachmentError):
                attachments.download(reopened, other, item["id"], session_settings.source_privacy)
        source = result["submission"]["input_message_id"]
        context_controls.save(
            store,
            sid,
            context_controls.Update(
                expected_revision=0,
                policy=context_controls.Policy(
                    sessionInstructions="",
                    messagePolicies={source: "exclude"},
                    budget=context_controls.Budget(
                        contextWindowTokens=20000, outputReserveTokens=100, otherInputTokens=0
                    ),
                ),
            ),
        )
        loop.run_turn(sid, "New question", request_id="image_request_02")
        assert not any(message.images for message in calls[-1])
        assert [
            image
            for message in turn_context.replay(store, result["turn_id"])["messages"]
            for image in message.images
        ] == images


def test_private_image_cannot_be_reused_with_weaker_privacy(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
                ),
            ),
        )
        item = attachments.upload(
            store,
            sid,
            attachments.Metadata(name="private.png", type="image/png"),
            picture(),
            resolve=session_settings.source_privacy,
        )
        with store._connect() as db, pytest.raises(attachments.AttachmentError) as error:
            attachment_turns._text(
                db, sid, item["id"], {"memoryDisabled": False, "harnessDisabled": False}
            )
        assert error.value.detail["code"] == "private_attachment"


def test_text_only_catalogue_model_never_receives_image(monkeypatch):
    adapter = OpenRouterProvider("synthetic")
    monkeypatch.setattr(
        adapter, "_guard_cost", lambda model: SimpleNamespace(supports_images=False)
    )
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: pytest.fail("Image sent to text-only model")
    )
    image = image_input.prepare(picture(), "image/png", "attachment_a")
    with pytest.raises(ProviderUnavailable, match="does not advertise"):
        adapter.complete([Message("user", "Describe", (image,))], model="text-only")
