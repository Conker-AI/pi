"""Hide secrets from online models (`pi.models.redaction`): what is found, what is left alone."""

import json
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing

import pytest
from made_up_secrets import (
    AWS_SHAPE,
    BEARER_SHAPE,
    CARD,
    DASHES,
    FINE_GRAINED_SHAPE,
    GITHUB_SHAPE,
    GOOGLE_SHAPE,
    HUGGING_FACE_SHAPE,
    JWT_SHAPE,
    KEY_LINES,
    MADE_UP_VALUE,
    NPM_SHAPE,
    OPENAI_SHAPE,
    SLACK_SHAPE,
    STRIPE_SHAPE,
    choose,
    pem,
)

from pi.conversation.store import Store
from pi.core.images import ImageInput
from pi.models import redaction
from pi.models.providers import Message, NotBilled


def cleaned(text):
    out, counts = redaction.redact(text)
    return out, dict(counts)


@pytest.mark.parametrize(
    ("text", "kind", "secret"),
    [
        (f"my key is {OPENAI_SHAPE}, keep it", "key", OPENAI_SHAPE),
        (f"push with {GITHUB_SHAPE}", "key", GITHUB_SHAPE),
        (f"or {FINE_GRAINED_SHAPE}", "key", FINE_GRAINED_SHAPE),
        (f"slack {SLACK_SHAPE}", "key", SLACK_SHAPE),
        (f"aws_access_key_id = {AWS_SHAPE}", "key", AWS_SHAPE),
        (f"stripe {STRIPE_SHAPE}", "key", STRIPE_SHAPE),
        (f"maps {GOOGLE_SHAPE}", "key", GOOGLE_SHAPE),
        (f"session {JWT_SHAPE}", "key", JWT_SHAPE),
        ("Authorization: Bear" + f"er {BEARER_SHAPE}", "key", BEARER_SHAPE),
        (f"hugging face {HUGGING_FACE_SHAPE}", "key", HUGGING_FACE_SHAPE),
        (f"npm {NPM_SHAPE}", "key", NPM_SHAPE),
        *(
            (name + "=" + MADE_UP_VALUE, "key", MADE_UP_VALUE)
            for name in ("GITHUB_TOKEN", "HF_TOKEN", "SLACK_TOKEN", "discord_token", "X-Api-Key")
        ),
        (f'"aws_secret_access_key": "{MADE_UP_VALUE}"', "key", MADE_UP_VALUE),
        ("DB_PASSWORD=hunter2 and more", "password", "hunter2"),
        ("password: pa55word", "password", "pa55word"),
        # After a password's name any value but a placeholder is one, short or plain.
        ("password=hunter", "password", "hunter"),
        ("password: dragon", "password", "dragon"),
        ("password=my.secret.pw", "password", "my.secret.pw"),
        ("SMTP_PASS=letmein", "password", "letmein"),
        ('passwd = "with spaces 1"', "password", "with spaces 1"),
        ("secret='made.up.1'", "password", "made.up.1"),
        ("client_secret: madeup123", "password", "madeup123"),
        (f"card {CARD} expires soon", "card", CARD),
        ("card 4111-1111-1111-1111.", "card", "4111-1111-1111-1111"),
        ("mastercard 5555555555554444", "card", "5555555555554444"),
        ("amex 3782 822463 10005", "card", "3782 822463 10005"),
        ("here it is:\n" + pem() + "\nthanks", "private_key", KEY_LINES[0]),
        (pem("OPENSSH PRIVATE KEY"), "private_key", KEY_LINES[1]),
        (
            pem("PGP PRIVATE KEY BLOCK", ["Version: Made Up 1", "", *KEY_LINES]),
            "private_key",
            "Made Up",
        ),
    ],
)
def test_each_kind_of_secret_is_replaced_by_its_marker(text, kind, secret):
    out, counts = cleaned(text)
    assert secret not in out
    assert redaction.MARKERS[kind] in out
    assert counts == {kind: 1}


@pytest.mark.parametrize(
    "text",
    [
        "Meet on 2026-11-25 at 10:30, or 25/11/2026 23:59:59.",
        "Call +1 415-555-2671, +44 20 7946 0958 or 0151 234 567 890 12.",
        "commit 4f2c1d0e9b8a7c6d5e4f3a2b1c0d9e8f7a6b5c4d, short 4f2c1d0",
        "ids 550e8400-e29b-41d4-a716-446655440000 and 12345678-1234-1234-1234-123456789012",
        "IBAN DE89 3704 0044 0532 0130 00, ISBN 978-3-16-148410-0",
        "at 1764028800000 ms, order 1234567812345670, total 4111111111111112",
        "4111111111111111.5 and 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 and 4111 11 1111 1111 111",
        "pip install scikit-learn; sk-learn-is-a-great-library-for-ml",
        "Bearer authentication-scheme-header, the bearer of good news",
        "password_length=12, PASSWORD_FILE=/run/secrets/db, has_password: true",
        "password: null, password: ${DB_PASSWORD}, password: '<your password>', password=****",
        "password: changeme, password=xxx, password: {{ vault_pw }}, password=%DB_PW%, pass: $PW",
        'password = getpass(), password: str, password: string; if password == "x"',
        "password=self.password, password=settings.DB_PASSWORD, password: config.get('db')",
        'secret = request.form.get("secret"), api_key: Optional[str]',
        "api_key=settings.OPENAI_API_KEY, api_key: os.environ['OPENAI_API_KEY']",
        # A key's name with a value too plain to be a key: code, counts and ids.
        "primary_key: id, sort_key=created_at, foreign_key: users.id, input_token: 1234",
        "max_tokens=4096, page_token=CAoQAA, hotkey: ctrl+shift+p",
        # pwd is also the working directory.
        "pwd: /home/user, OLDPWD=C:\\Users\\owner, PWD=/srv/conker",
        DASHES + "BEGIN PRIVATE KEY" + DASHES + " is how such a block starts",
        "monkey: banana, keyboard=qwerty",
    ],
)
def test_ordinary_numbers_ids_and_code_are_left_alone(text):
    assert cleaned(text) == (text, {})


def test_a_key_inside_a_code_block_is_removed():
    out, counts = cleaned("Deploy key:\n```\n" + pem() + "\n```\nPlease keep it.")
    assert out == "Deploy key:\n```\n[removed: private key]\n```\nPlease keep it."
    assert counts == {"private_key": 1}


def test_each_half_of_a_key_cut_between_two_messages_is_removed():
    block = pem()
    cut = block.index(KEY_LINES[2])
    sent, counts = redaction.outgoing(
        [Message("user", "first half: " + block[:cut]), Message("user", block[cut:] + " done")]
    )
    assert [m.content for m in sent] == [
        "first half: [removed: private key]\n",
        "[removed: private key] done",
    ]
    assert counts == {"private_key": 2}


def test_secrets_in_a_tool_result_go_and_its_json_stays_valid():
    result = json.dumps(
        {
            "tool": "files.read",
            "result": {
                "text": ".env:\nDB_PASSWORD=hunter2\nOPENAI=" + OPENAI_SHAPE + "\n" + pem(),
                "card": "4111111111111111",
                "body": json.dumps({"password": "pa55word", "note": "password:"}),
            },
        },
        ensure_ascii=False,
    )
    out, counts = cleaned(result)
    parsed = json.loads(out)
    text = parsed["result"]["text"]
    assert (
        text
        == ".env:\nDB_PASSWORD=[removed: password]\nOPENAI=[removed: key]\n[removed: private key]"
    )
    assert parsed["result"]["card"] == "[removed: card number]"
    assert json.loads(parsed["result"]["body"]) == {
        "password": "[removed: password]",
        "note": "password:",
    }
    assert counts == {"password": 2, "key": 1, "private_key": 1, "card": 1}


@pytest.mark.parametrize(
    "document",
    [
        {"tool": "t_echo", "args": {"text": "password:", "other": "sk-x", "password": "abc12345"}},
        {"a": "password=", "b": ["password: ", '"', "\\"], "secret": {"nested": "pa55word"}},
        {"lines": "key\\n" + GITHUB_SHAPE + "\\t" + CARD, "pem": pem(newline="\\n")},
        [{"password": 'quo"te\\d 1'}, "secret=" + "\\u00e9t\\u00e91234", "pwd=a\\\\b1"],
    ],
)
def test_redaction_never_breaks_json(document):
    for ascii_only in (True, False):
        text = json.dumps(document, ensure_ascii=ascii_only)
        out, _ = cleaned(text)
        json.loads(out)
        json.loads(cleaned(json.dumps(out))[0])


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_an_unquoted_json_value_is_replaced_by_a_quoted_marker(depth):
    text = json.dumps({"password": 12345678, "card": 4111111111111111, "n": 1})
    for _ in range(depth):
        text = json.dumps(text)
    out, counts = cleaned(text)
    for _ in range(depth + 1):
        out = json.loads(out)
    assert out == {"password": "[removed: password]", "card": "[removed: card number]", "n": 1}
    assert counts == {"password": 1, "card": 1}


def test_the_rest_of_a_key_cut_by_a_line_wrap_goes_too():
    rest = "fake1" * 6
    out, counts = cleaned(f"my key is {OPENAI_SHAPE}\n{rest}\nThanks")
    assert out == "my key is [removed: key]\nThanks" and counts == {"key": 1}
    escaped, _ = cleaned(json.dumps({"text": f"{GITHUB_SHAPE}\n{rest}\n{rest}\nok"}))
    assert json.loads(escaped) == {"text": "[removed: key]\nok"}
    # A word on the next line is not part of the key.
    assert cleaned(f"{OPENAI_SHAPE}\nRegards")[0] == "[removed: key]\nRegards"


def test_redacting_twice_changes_nothing_more():
    text = " ".join(
        [OPENAI_SHAPE, "password: pa55word", f'"secret": "{GITHUB_SHAPE}"', CARD, pem()]
    )
    once, _ = cleaned(text)
    assert cleaned(once) == (once, {})


def test_the_messages_given_are_never_changed():
    image = ImageInput(media_type="image/png", data="aGVsbG8=")
    given = [
        Message("system", "memory: the owner's card is " + CARD),
        Message("user", "use " + OPENAI_SHAPE, (image,)),
        Message("assistant", "plain words"),
    ]
    before = [(m.role, m.content, m.images) for m in given]
    sent, counts = redaction.outgoing(given)
    assert [(m.role, m.content, m.images) for m in given] == before
    assert sent is not given and sent[2] is given[2]
    assert [m.content for m in sent[:2]] == [
        "memory: the owner's card is [removed: card number]",
        "use [removed: key]",
    ]
    assert sent[1].images == (image,)
    assert counts == {"card": 1, "key": 1}


@pytest.mark.parametrize(
    "text",
    [
        ("FAKE" * 16 + "\n") * 31_000 + DASHES + "END PRIVATE KEY" + DASHES,
        (DASHES + "BEGIN PRIVATE KEY" + DASHES + "\n") * 70_000,
        "4" * 2_000_000,
        "4111 " * 400_000,
        "password:$password:$" * 100_000,
        "a" * 2_000_000,
        "sk-" * 700_000,
        ("x:" * 50 + "\n") * 20_000,
    ],
    ids=["key-lines", "headers", "digits", "groups", "assignments", "word", "prefixes", "colons"],
)
def test_two_megabytes_of_hostile_text_takes_linear_time(text):
    started = time.perf_counter()
    redaction.redact(text)
    # Linear passes take well under a second here; a quadratic one takes minutes.
    assert time.perf_counter() - started < 10


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_off_unless_the_owner_turns_it_on(store):
    assert redaction.DEFAULT is False and not redaction.enabled(store)
    choose(store, {"privacy.redact": True})
    assert redaction.enabled(store)
    choose(store, {"privacy.redact": False})
    assert not redaction.enabled(store)


@pytest.mark.parametrize("stored", ["true", 1, "on", "yes", "false", 0, None, "", [], {}])
def test_only_a_stored_false_turns_it_off(store, stored):
    choose(store, {"privacy.redact": stored})
    assert redaction.enabled(store)


def test_a_setting_that_cannot_be_read_counts_as_on(store):
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("UPDATE owner_settings SET vals='not json'")
        db.commit()
    assert redaction.enabled(store)


class Adapter:
    def __init__(self, name, url=None):
        self.name, self.url, self.sent = name, url, []

    def complete_bounded(self, messages, *, model, timeout):
        self.sent.append(messages)
        return "answer"


def test_a_request_that_could_not_be_cleaned_is_not_sent(store, monkeypatch):
    choose(store, {"privacy.redact": True})
    adapter = Adapter("openai")

    def broken(text):
        raise RuntimeError("made-up failure " + text)

    monkeypatch.setattr(redaction, "redact", broken)
    with pytest.raises(NotBilled) as refused:
        redaction.Redacting(adapter, store).complete_bounded(
            [Message("user", OPENAI_SHAPE)], model="m", timeout=5
        )
    assert adapter.sent == [] and OPENAI_SHAPE not in str(refused.value)


def test_two_calls_at_once_each_count_only_their_own(store):
    choose(store, {"privacy.redact": True})
    adapter = redaction.Redacting(Adapter("openai"), store)
    together = threading.Barrier(2)
    seen = {}

    def call(name, text):
        counts = Counter()
        with redaction.tally(counts):
            together.wait(timeout=30)
            adapter.complete_bounded([Message("user", text)], model="m", timeout=5)
        seen[name] = dict(counts)

    threads = [
        threading.Thread(target=call, args=("one", OPENAI_SHAPE)),
        threading.Thread(target=call, args=("two", f"{CARD} and {CARD}")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert seen == {"one": {"key": 1}, "two": {"card": 2}}


class Ollama(Adapter):
    def __init__(self, base_url):
        super().__init__("ollama")
        self.base_url = base_url


def test_every_adapter_that_can_send_off_this_server_is_wrapped(store):
    local = Adapter("decisions", "http://127.0.0.1:8060")
    remote = Adapter("decisions", "https://decisions.example.test")
    ollama, hosted, providers = redaction.wrap(
        store,
        Ollama("http://ollama:11434"),
        Adapter("openrouter"),
        {"decisions": local, "remote": remote, "openai": Adapter("x")},
    )
    assert isinstance(ollama, redaction.Redacting) and isinstance(hosted, redaction.Redacting)
    assert providers["decisions"] is local
    assert isinstance(providers["remote"], redaction.Redacting)
    assert isinstance(providers["openai"], redaction.Redacting)
    assert redaction.wrap(store, None, None, {}) == (None, None, {})
    # Everything but the request reads through to the adapter.
    assert providers["remote"].url == "https://decisions.example.test"
    with pytest.raises(AttributeError):
        _ = providers["remote"]._private
    # Nor anything that would send text around the cleaned copy.
    for name in ("choose", "rank_memories", "client", "transport"):
        setattr(remote, name, lambda *args, **kwargs: "sent")
        with pytest.raises(AttributeError):
            getattr(providers["remote"], name)


@pytest.mark.parametrize(
    ("url", "model", "redacted"),
    [
        ("http://ollama:11434", "qwen3:4b", False),  # its service on this server's network
        ("http://127.0.0.1:11434", "llama3.2", False),
        ("http://localhost:11434", "llama3.2", False),
        ("http://[::1]:11434", "llama3.2", False),
        ("http://ollama:11434", "gpt-oss:120b-cloud", True),  # Ollama answers these online
        ("http://ollama:11434", "glm-4.6:cloud", True),
        ("http://127.0.0.1:11434", "kimi-k2:1t-cloud", True),
        ("http://ollama.example.test:11434", "qwen3:4b", True),
        ("http://10.0.0.5:11434", "qwen3:4b", True),
        ("http://ollama-gpu:11434", "qwen3:4b", True),
        ("not a url", "qwen3:4b", True),
    ],
)
def test_ollama_is_local_only_on_this_server_and_for_local_models(store, url, model, redacted):
    choose(store, {"privacy.redact": True})
    adapter = Ollama(url)
    wrapped, _, _ = redaction.wrap(store, adapter, None, {})
    wrapped.complete_bounded([Message("user", "use " + OPENAI_SHAPE)], model=model, timeout=5)
    assert (OPENAI_SHAPE not in adapter.sent[0][0].content) is redacted
