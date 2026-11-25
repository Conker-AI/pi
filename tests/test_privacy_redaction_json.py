"""Hide secrets from online models (`pi.models.redaction`) in JSON: a secret at any depth of
JSON written inside JSON strings goes, and JSON that was valid stays valid at every depth."""

import json
import random
import string

import pytest
from made_up_secrets import DASHES

from pi.models import redaction

HIDDEN = "Made" + "Up" + "pw1"  # stands for a password
FAKE_CARD = 4111111111111111  # the card networks' published test number


def cleaned(text):
    out, counts = redaction.redact(text)
    return out, dict(counts)


def inside(text, depth):
    """`text` as the output of a tool that printed it, `depth` times over."""
    for _ in range(depth):
        text = json.dumps({"stdout": text})
    return text


def outside(text, depth):
    for _ in range(depth):
        text = json.loads(text)["stdout"]
    return text


def same_shape(before, after):
    """Fails unless `after` holds valid JSON wherever `before` did, at every depth, or the whole
    value went (a key's name with a JSON document as its value)."""
    if isinstance(before, str):
        if after in redaction.MARKERS.values():
            return
        try:
            inner = json.loads(before)
        except ValueError:
            return
        if isinstance(inner, (dict, list)):
            same_shape(inner, json.loads(after))
    elif isinstance(before, dict):
        assert isinstance(after, dict) and after.keys() == before.keys()
        for name, value in before.items():
            same_shape(value, after[name])
    elif isinstance(before, list):
        assert isinstance(after, list) and len(after) == len(before)
        for value, kept in zip(before, after, strict=True):
            same_shape(value, kept)


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_a_password_in_json_inside_json_strings_goes_and_the_json_stays_valid(depth):
    document = {"cfg": f'password: "{HIDDEN}"', "password": HIDDEN, "note": "ok"}
    out, counts = cleaned(inside(json.dumps(document), depth))
    assert HIDDEN not in out and counts == {"password": 2}
    assert json.loads(outside(out, depth)) == {
        "cfg": 'password: "[removed: password]"',
        "password": "[removed: password]",
        "note": "ok",
    }


def test_a_password_in_kubectls_last_applied_configuration_goes():
    applied = {"kind": "Secret", "stringData": {"password": HIDDEN}}
    secret = {
        "kind": "Secret",
        "metadata": {
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": json.dumps(applied)}
        },
    }
    out, counts = cleaned(inside(json.dumps(secret, indent=2), 1))
    assert HIDDEN not in out and counts == {"password": 1}
    annotations = json.loads(outside(out, 1))["metadata"]["annotations"]
    shown = json.loads(annotations["kubectl.kubernetes.io/last-applied-configuration"])
    assert shown == {"kind": "Secret", "stringData": {"password": "[removed: password]"}}


@pytest.mark.parametrize(
    "value",
    ['quo"te', "back\\slash", 'both\\"', "ends\\", '"', "\\"],
    ids=["quote", "backslash", "both", "trailing-backslash", "only-quote", "only-backslash"],
)
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_a_quoted_password_with_escapes_goes_whole(value, depth):
    document = {"password": HIDDEN + value, "next": "v", "cfg": f'password="{HIDDEN}"'}
    out, counts = cleaned(inside(json.dumps(document), depth))
    assert HIDDEN not in out and counts == {"password": 2}
    assert json.loads(outside(out, depth)) == {
        "password": "[removed: password]",
        "next": "v",
        "cfg": 'password="[removed: password]"',
    }


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"cards": [FAKE_CARD]}, {"cards": ["[removed: card number]"]}),
        ({"a": 1, "b": [1, FAKE_CARD]}, {"a": 1, "b": [1, "[removed: card number]"]}),
        ([FAKE_CARD], ["[removed: card number]"]),
        ({"x": ["a", FAKE_CARD, {"y": FAKE_CARD}]}, None),
        ({"x": f"[{FAKE_CARD}]"}, {"x": '["[removed: card number]"]'}),
    ],
    ids=["array", "after-comma", "top-level", "mixed", "json-in-a-string"],
)
@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_a_card_number_in_a_json_array_becomes_a_quoted_marker(document, expected, indent, depth):
    if expected is None:
        expected = {"x": ["a", "[removed: card number]", {"y": "[removed: card number]"}]}
    out, _ = cleaned(inside(json.dumps(document, indent=indent), depth))
    assert str(FAKE_CARD) not in out
    assert json.loads(outside(out, depth)) == expected


def test_json_that_was_valid_stays_valid_at_every_depth():
    chance = random.Random(7)

    def word(size, letters=string.ascii_letters + string.digits):
        return "".join(chance.choice(letters) for _ in range(size))

    def block():
        lines = [word(64, string.ascii_letters + string.digits + "+/") for _ in range(3)]
        key = "PRIVATE KEY" + DASHES
        return "\n".join([DASHES + "BEGIN " + key, *lines, word(10) + "==", DASHES + "END " + key])

    shapes = [
        lambda: "sk" + "-proj-" + word(40),
        lambda: "gh" + "p_" + word(36),
        lambda: str(FAKE_CARD),
        lambda: "4111 1111 1111 1111",
        block,
        lambda: "Bear" + "er " + word(30),
        lambda: "password=" + word(10),
        lambda: 'password: "x' + word(8) + '"',
        lambda: "api_key: " + word(20) + "9",
        lambda: 'a\b"c sk' + "-" + word(25) + "1\\",
        lambda: (
            chr(0xE9)
            + chr(0x4E2D)
            + " AK"
            + "IA"
            + word(16, string.ascii_uppercase + string.digits)
        ),
    ]
    names = ["password", "api_key", "card", "secret", "token", "x", "pwd", "note", "access-key"]

    def value(depth):
        roll = chance.random()
        if roll < 0.15:
            return chance.choice([FAKE_CARD, 12345678, 378282246310005, True, None, 3.5])
        if roll < 0.25 and depth < 3:
            return json.dumps(document(depth + 1), ensure_ascii=chance.random() < 0.5)
        if roll < 0.35 and depth < 3:
            return document(depth + 1)
        if roll < 0.45:
            return [chance.choice(shapes)(), FAKE_CARD]
        start, end = chance.choice(["", "plain ", "x: "]), chance.choice(["", " end", "\\", '"'])
        return start + chance.choice(shapes)() + end

    def document(depth=0):
        return {
            chance.choice(names) + chance.choice(["", "_2"]): value(depth)
            for _ in range(chance.randint(1, 4))
        }

    for _ in range(600):
        text = json.dumps(
            document(), ensure_ascii=chance.random() < 0.5, indent=chance.choice([None, 2])
        )
        for _ in range(chance.randint(0, 2)):
            text = json.dumps(text)
        out, _ = cleaned(text)
        before, after = json.loads(text), json.loads(out)
        while isinstance(before, str):
            before, after = json.loads(before), json.loads(after)
        same_shape(before, after)
