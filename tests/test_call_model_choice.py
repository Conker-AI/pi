"""A call's model choice: it applies from the next accepted request, survives
updates that do not name it, and an interruption stops the configured fallback."""

from test_calls import Provider, Speech, send, start, store  # noqa: F401

from pi.calls import call_models, call_turns
from pi.calls import runtime as c
from pi.conversation import settings as session_settings
from pi.models.providers import ProviderUnavailable
from pi.models.routing import Router
from pi.turns.loop import Loop


def test_model_preference_changes_only_future_accepted_requests(store):
    from test_model_roles import config

    from pi.models import roles as model_roles

    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )
    call = start(store)
    call = c.update(
        store,
        call["id"],
        call_models.Update(
            expected_revision=1,
            modelId="a",
            channels=call_models.ChannelUpdate(microphone=True, voice=False),
        ),
    )
    first, second = Provider(), Provider()
    second.name = "two"
    loop = Loop(store, Router(local_provider=first, local_model="a", hosted_provider=second))
    speech = Speech(
        callback=lambda: c.update(
            store, call["id"], call_models.Update(expected_revision=2, modelId="b")
        )
    )
    result = call_turns.run(
        store,
        loop,
        call["id"],
        call_models.Send(request_id="model_freeze_857300"),
        speech=speech,
        audio=b"synthetic pcm",
    )
    assert result["call"]["requests"][0]["state"] == "complete"
    assert first.seen[0][1] == "actual-a" and not second.seen
    send(store, loop, call, 2)
    assert second.seen[0][1] == "actual-b"
    assert session_settings.load(store, call["conversationId"])["settings"].get("modelId") is None


def test_an_update_that_does_not_name_a_model_keeps_the_chosen_one(store):
    from test_model_roles import config

    from pi.models import roles as model_roles

    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )
    call = start(store)
    call = c.update(store, call["id"], call_models.Update(expected_revision=1, modelId="a"))
    assert call["modelId"] == "a"
    for change in (
        {"paused": True},
        {"paused": False},
        {"mode": "character"},
        {"channels": call_models.ChannelUpdate(microphone=False)},
    ):
        call = c.update(
            store,
            call["id"],
            call_models.Update(expected_revision=call["revision"], **change),
        )
        assert call["modelId"] == "a", change
    call = c.update(
        store, call["id"], call_models.Update(expected_revision=call["revision"], modelId=None)
    )
    assert call["modelId"] is None


def test_interruption_blocks_configured_model_fallback(store):
    from test_model_roles import config

    from pi.models import roles as model_roles

    configuration = config()
    configuration["roleSettings"]["answerMode"] = "router"
    assignment = dict(
        configuration["roleSettings"]["roles"]["answer"], failure="fallback", fallbackModelId="b"
    )
    configuration["roleSettings"]["roles"]["routing"] = assignment
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0,
            configuration=model_roles.Configuration.model_validate(configuration),
        ),
    )
    call = start(store)

    def stop_then_fail():
        c.interrupt(store, call["id"], call_models.Revision(expected_revision=1))
        raise ProviderUnavailable("synthetic provider outage")

    first, second = Provider(stop_then_fail), Provider()
    second.name = "two"
    loop = Loop(store, Router(local_provider=first, local_model="a", hosted_provider=second))
    result = send(store, loop, call)
    assert result["call"]["requests"][0]["state"] == "failed"
    assert len(first.seen) == 1 and not second.seen
