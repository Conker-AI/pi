"""Owner notification settings for web push: days kept free of notifications and notification
text. Each test fails if its reader ignores the value (round four of R-F1)."""

import json
from contextlib import closing
from datetime import timedelta

import pytest

from gateway import push
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from tests.test_gateway_push import FCM, decrypt, device, subscribe, world  # noqa: F401
from tests.test_push_queue import DAY, approval, change, claim, uncertain


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


# notifications.quiet_days


def test_a_quiet_day_holds_every_item_until_the_next_day(store):
    uncertain(store)
    approval(store)
    change(store, quiet={"urgentExceptions": True})
    # DAY is a Monday afternoon in the owner's time zone.
    choose(store, {"notifications.quiet_days": ["monday", "sunday"]})
    held = claim(store)
    assert held["items"] == [] and held["held"] == ["quiet_day"], "urgent ones too"
    assert len(claim(store, now=DAY + timedelta(days=1))["items"]) == 2


def test_other_days_and_unreadable_values_hold_nothing(store):
    approval(store)
    choose(store, {"notifications.quiet_days": "monday"})
    assert len(claim(store)["items"]) == 1
    approval(store)
    choose(store, {"notifications.quiet_days": ["tuesday", 3]})
    assert len(claim(store)["items"]) == 1


# notifications.preview


def test_with_notification_text_off_a_claim_says_so(store):
    approval(store)
    first = claim(store)["items"]
    assert set(first[0]) == {"kind", "urgent", "tag"}, "on by default, as before"
    choose(store, {"notifications.preview": False})
    approval(store)
    assert claim(store)["items"][0]["preview"] is False


def test_with_notification_text_off_the_lock_screen_shows_only_that_conker_needs_you(world):
    client, headers, state, app, _ = world
    key, p256dh, secret = device()
    subscribe(client, headers, FCM, p256dh, secret)
    app.state.push.notices.clear()
    titles = []
    for tag, preview in (("a", None), ("b", False), ("c", "no")):
        item = {"kind": "approval", "urgent": False, "tag": tag * 16}
        state.claim = {"items": [item if preview is None else {**item, "preview": preview}]}
        state.pushes.clear()
        app.state.push.tick()
        titles.append(decrypt(state.pushes[-1].content, key, p256dh, secret)["title"])
    assert titles == [push.TITLES["approval"], push.GENERIC, push.GENERIC]
    assert (
        json.loads(push.message({"kind": "approval", "tag": "d" * 16}))["title"]
        == (push.TITLES["approval"])
    )
