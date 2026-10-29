"""The owner's chat settings Pi reads: new-chat privacy, chat names, the queue and uploads.

Each setting is checked three ways: a new chat (or a new message, name or file) follows it, what
already exists does not change, and a value of the wrong type or out of range counts as the
setting's default.
"""

from contextlib import closing

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from test_naming import Recording, configured

from pi.api.routes.attachments import router as attachment_router
from pi.content import attachment_files
from pi.content import attachments as a
from pi.conversation import chat_preferences, naming
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.core import errors
from pi.owner import settings as owner_settings
from pi.schedule import tasks
from pi.turns import queue as q

MIB = 1024 * 1024
resolve = session_settings.source_privacy


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, **values):
    """Save the owner's settings document with these values (dots written as __)."""
    current = owner_settings.load(store)["revision"]
    owner_settings.save(
        store,
        owner_settings.Update(
            expected_revision=current,
            values={key.replace("__", "."): value for key, value in values.items()},
        ),
    )


def privacy(store, sid):
    return session_settings.load(store, sid)["settings"]["privacy"]


def read(store, reader):
    with store._connect() as db:
        return reader(db)


# --- chats.default_incognito and privacy.helpers ---


def test_new_chats_start_open_when_nothing_is_set(store):
    sid = store.create_session()
    assert privacy(store, sid) == {"memoryDisabled": False, "harnessDisabled": False}
    with store._connect() as db:
        # No settings row is written, exactly as before these settings were read.
        assert not db.execute(
            "SELECT 1 FROM session_settings WHERE session_id=?", (sid,)
        ).fetchone()


def test_private_by_default_turns_memory_off_and_keeps_new_chats_out_of_search(store):
    choose(store, chats__default_incognito=True)
    sid = store.create_session()
    assert privacy(store, sid) == {"memoryDisabled": True, "harnessDisabled": False}
    with store._connect() as db:
        assert session_settings.source_privacy(db, sid)["incognito"] is True


def test_helpers_off_turns_helpers_off_in_new_chats(store):
    choose(store, privacy__helpers=False)
    sid = store.create_session()
    assert privacy(store, sid) == {"memoryDisabled": False, "harnessDisabled": True}


def test_new_chat_defaults_leave_existing_chats_and_forks_unchanged(store):
    before = store.create_session()
    choose(store, chats__default_incognito=True, privacy__helpers=False)
    after = store.create_session()
    child = store.create_session(parent_id=before)
    assert privacy(store, before) == {"memoryDisabled": False, "harnessDisabled": False}
    assert privacy(store, child) == {"memoryDisabled": False, "harnessDisabled": False}
    assert privacy(store, after) == {"memoryDisabled": True, "harnessDisabled": True}
    choose(store)
    # Turning the defaults back off changes no chat either: each chat kept what it started with.
    assert privacy(store, after) == {"memoryDisabled": True, "harnessDisabled": True}


def test_a_choice_made_for_the_chat_wins_over_the_default(store):
    choose(store, chats__default_incognito=True, privacy__helpers=False)
    sid = store.create_session(privacy={"memoryDisabled": False})
    assert privacy(store, sid) == {"memoryDisabled": False, "harnessDisabled": True}


def test_new_chat_defaults_add_to_the_memory_choice_from_setup(store):
    with store._connect() as db:
        db.execute("INSERT INTO setup_optional_choices VALUES('memory',1,'choice_skip','skip',0)")
    choose(store, privacy__helpers=False)
    sid = store.create_session()
    assert privacy(store, sid) == {"memoryDisabled": True, "harnessDisabled": True}


@pytest.mark.parametrize(
    "incognito,helpers", [("true", "false"), (1, 0), (None, None), ([True], []), ({"on": 1}, "")]
)
def test_odd_privacy_values_count_as_the_defaults(store, incognito, helpers):
    choose(store, chats__default_incognito=incognito, privacy__helpers=helpers)
    assert read(store, chat_preferences.new_chat_privacy) == {
        "memoryDisabled": False,
        "harnessDisabled": False,
    }
    assert privacy(store, store.create_session()) == {
        "memoryDisabled": False,
        "harnessDisabled": False,
    }


# --- chats.names.auto and chats.names.style ---


def chat_with_answer(store):
    sid = store.create_session()
    store.append_message(sid, "user", "Which tent should I buy?")
    store.append_message(sid, "assistant", "A light two-person tent.")
    return sid


def test_automatic_names_off_stops_naming_after_the_answer(store):
    adapter = Recording('{"title": "Tent advice"}')
    configured(store)
    choose(store, chats__names__auto=False)
    sid = chat_with_answer(store)
    naming.after_turn(store, {"one": adapter}, sid)
    assert adapter.calls == []
    assert naming.load(store, sid)["title"] == ""
    # Asking for a name is the owner's own request, so it still works.
    named = naming.auto_name(store, {"one": adapter}, sid, naming.AutoName(expected_revision=0))
    assert named["title"] == "Tent advice"


@pytest.mark.parametrize("value", [None, True, "false", 0])
def test_automatic_names_stay_on_unless_turned_off(store, value):
    adapter = Recording('{"title": "Tent advice"}')
    configured(store)
    choose(store, chats__names__auto=value)
    sid = chat_with_answer(store)
    naming.after_turn(store, {"one": adapter}, sid)
    assert naming.load(store, sid)["title"] == "Tent advice"


def test_automatic_names_off_leaves_existing_names(store):
    adapter = Recording('{"title": "Tent advice"}')
    configured(store)
    sid = chat_with_answer(store)
    naming.after_turn(store, {"one": adapter}, sid)
    choose(store, chats__names__auto=False)
    naming.after_turn(store, {"one": adapter}, sid)
    named = naming.load(store, sid)
    assert (named["revision"], named["title"], named["source"]) == (1, "Tent advice", "auto")


@pytest.mark.parametrize(
    "style,rule",
    [
        ("short", "2 to 6 words"),
        ("descriptive", "4 to 10 words that say what the conversation is about"),
        ("emoji", "one fitting emoji, a space, then 2 to 5 words"),
    ],
)
def test_name_style_sets_the_naming_rule(store, style, rule):
    adapter = Recording('{"title": "Tent advice"}')
    configured(store)
    choose(store, chats__names__style=style)
    sid = chat_with_answer(store)
    naming.after_turn(store, {"one": adapter}, sid)
    instructions = adapter.messages[0][0].content
    assert instructions == naming.STYLES[style]
    assert rule in instructions and "at most 60 characters" in instructions


@pytest.mark.parametrize("value", ["Emoji", "long", 3, True, None, ["emoji"]])
def test_unknown_name_styles_are_short(store, value):
    choose(store, chats__names__style=value)
    assert read(store, chat_preferences.name_style) == "short"


# --- chats.queue and chats.queue_max ---


def message(number):
    return q.Enqueue(request_id=f"queue_request_{number:04d}", text=f"Question {number}")


def test_queue_off_refuses_new_messages_and_keeps_waiting_ones(store):
    sid = store.create_session()
    waiting = q.enqueue(store, sid, message(0))
    choose(store, chats__queue=False)
    with pytest.raises(tasks.TaskError, match="off in Settings") as refused:
        q.enqueue(store, sid, message(1))
    assert refused.value.code == "queue_off"
    # Sending the same request again is a replay, not a new message.
    assert q.enqueue(store, sid, message(0)) == waiting
    assert [entry["id"] for entry in q.read(store, sid)["entries"]] == [waiting["id"]]


def test_queued_messages_sets_how_many_can_wait(store):
    choose(store, chats__queue_max=2)
    sid = store.create_session()
    q.enqueue(store, sid, message(0))
    q.enqueue(store, sid, message(1))
    with pytest.raises(tasks.TaskError, match="at most two messages") as refused:
        q.enqueue(store, sid, message(2))
    assert refused.value.code == "queue_full"
    other = store.create_session()
    q.enqueue(store, other, message(3))
    choose(store, chats__queue_max=1)
    with pytest.raises(tasks.TaskError, match="at most one message"):
        q.enqueue(store, other, message(4))


def test_lowering_queued_messages_keeps_what_already_waits(store):
    sid = store.create_session()
    for number in range(4):
        q.enqueue(store, sid, message(number))
    choose(store, chats__queue_max=2)
    assert len(q.read(store, sid)["entries"]) == 4
    with pytest.raises(tasks.TaskError, match="two messages"):
        q.enqueue(store, sid, message(9))


@pytest.mark.parametrize("value", [0, 6, -1, 2.5, True, "3", None, [2]])
def test_odd_queue_sizes_count_as_five(store, value):
    choose(store, chats__queue_max=value)
    assert read(store, chat_preferences.queue_max) == 5
    sid = store.create_session()
    for number in range(5):
        q.enqueue(store, sid, message(number))
    with pytest.raises(tasks.TaskError, match="five messages"):
        q.enqueue(store, sid, message(5))


@pytest.mark.parametrize("value", [3, 3.0])
def test_whole_queue_sizes_are_used(store, value):
    choose(store, chats__queue_max=value)
    assert read(store, chat_preferences.queue_max) == 3


@pytest.mark.parametrize("value", [None, 0, "false", "off"])
def test_queue_stays_on_unless_turned_off(store, value):
    choose(store, chats__queue=value)
    assert read(store, chat_preferences.queue_on) is True


# --- files.upload_limit ---


def upload(store, sid, size):
    return a.upload(store, sid, a.Metadata(name="data.bin", type=""), b"x" * size, resolve)


def no_incoming_files(store):
    folder = attachment_files.blob_dir(store) / ".incoming"
    return not folder.exists() or not any(folder.iterdir())


def test_uploads_have_no_limit_until_the_owner_chooses_one(store):
    assert read(store, chat_preferences.upload_limit) is None
    sid = store.create_session()
    assert upload(store, sid, 2 * MIB)["size"] == 2 * MIB


def test_largest_upload_refuses_bigger_files(store):
    choose(store, files__upload_limit=1)
    sid = store.create_session()
    assert upload(store, sid, MIB)["size"] == MIB
    with pytest.raises(a.AttachmentError, match="at most 1 MB") as refused:
        upload(store, sid, MIB + 1)
    assert (refused.value.code, refused.value.status) == ("upload_limit", 413)
    assert no_incoming_files(store)
    assert len(a.listing(store, sid, resolve)) == 1


def test_largest_upload_leaves_files_already_shared(store):
    sid = store.create_session()
    kept = upload(store, sid, 2 * MIB)
    choose(store, files__upload_limit=1)
    assert a.get(store, sid, kept["id"], resolve)["size"] == 2 * MIB
    _, raw = a.download(store, sid, kept["id"], resolve)
    assert len(raw) == 2 * MIB


@pytest.mark.parametrize("value", [0, 501, -5, 1.5, True, "10", None, [10]])
def test_odd_upload_limits_count_as_no_limit(store, value):
    choose(store, files__upload_limit=value)
    assert read(store, chat_preferences.upload_limit) is None


@pytest.mark.parametrize("value,expected", [(1, MIB), (500, 500 * MIB), (25.0, 25 * MIB)])
def test_upload_limits_from_one_to_five_hundred_megabytes(store, value, expected):
    choose(store, files__upload_limit=value)
    assert read(store, chat_preferences.upload_limit) == expected


def owner_client(store):
    def authorize(key: str | None = Header(default=None)):
        if key != "owner":
            raise HTTPException(403)

    app = FastAPI()
    errors.install(app)
    app.include_router(attachment_router(lambda: store, authorize, resolve))
    return TestClient(app)


def test_upload_route_refuses_a_declared_size_over_the_limit(store):
    choose(store, files__upload_limit=1)
    sid = store.create_session()
    response = owner_client(store).post(
        f"/sessions/{sid}/attachments",
        headers={"key": "owner"},
        params={"name": "big.bin"},
        content=b"x" * (MIB + 1),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_limit"
    assert no_incoming_files(store)
    assert a.listing(store, sid, resolve) == []


def test_upload_route_stops_a_stream_once_it_passes_the_limit(store):
    choose(store, files__upload_limit=1)
    sid = store.create_session()

    def chunks():  # no declared length: the limit is checked as the file arrives
        for _ in range(4):
            yield b"x" * (MIB // 2)

    response = owner_client(store).post(
        f"/sessions/{sid}/attachments",
        headers={"key": "owner"},
        params={"name": "big.bin"},
        content=chunks(),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_limit"
    assert no_incoming_files(store)
    assert a.listing(store, sid, resolve) == []


def test_upload_route_accepts_a_file_within_the_limit(store):
    choose(store, files__upload_limit=1)
    sid = store.create_session()
    response = owner_client(store).post(
        f"/sessions/{sid}/attachments",
        headers={"key": "owner"},
        params={"name": "small.txt", "type": "text/plain"},
        content=b"hello",
    )
    assert response.status_code == 200
    assert response.json()["size"] == 5
