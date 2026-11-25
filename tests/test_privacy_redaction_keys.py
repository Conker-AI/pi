"""Hide secrets from online models: private key blocks (`pi.models.redaction_pem`), in every
wrapping a tool or a pasted message gives them, found in one linear pass."""

import json
import time

import pytest
from made_up_secrets import DASHES, KEY_LINES, pem

from pi.models import redaction


def cleaned(text):
    out, counts = redaction.redact(text)
    return out, dict(counts)


def leaked(out):
    return [line for line in KEY_LINES if line in out or line[:12] in out]


def nested(value, depth):
    for _ in range(depth):
        value = json.dumps(value)
    return value


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_a_key_in_json_inside_json_strings_is_removed_and_the_json_stays_valid(depth):
    # A tool reading a JSON file, or terraform output -json, wraps the key's line breaks in one
    # more backslash at each depth.
    for newline in ("\n", "\r\n"):
        text = nested({"private_key_pem": {"value": pem(newline=newline) + newline}}, depth)
        out, counts = cleaned(text)
        assert leaked(out) == [] and counts == {"private_key": 1}
        value = out
        for _ in range(depth):
            value = json.loads(value)
        assert value["private_key_pem"]["value"] == "[removed: private key]" + newline


@pytest.mark.parametrize(
    "wrap",
    [
        lambda block: "> " + block.replace("\n", "\n> "),  # a quoted reply
        lambda block: ">> " + block.replace("\n", "\n>> "),
        lambda block: block.replace("\n", "<br>"),  # HTML
        lambda block: block.replace("\n", "<br />\n"),
        lambda block: "# " + block.replace("\n", "\n# "),  # commented out
        lambda block: "// " + block.replace("\n", "\n// "),
        lambda block: "    " + block.replace("\n", "\n    "),  # indented
        lambda block: block.replace("\n", " "),  # on one line
    ],
    ids=["quoted", "quoted-twice", "br", "br-newline", "hash", "slashes", "indented", "one-line"],
)
def test_a_key_whose_lines_carry_a_prefix_or_markup_is_removed(wrap):
    out, counts = cleaned("Here it is:\n" + wrap(pem()) + "\nThanks.")
    assert leaked(out) == [] and counts == {"private_key": 1}
    assert out.startswith("Here it is:\n") and out.endswith("\nThanks.")


def test_a_short_last_line_and_a_checksum_go_with_the_block():
    lines = [*KEY_LINES[:3], "FAKE0f", "=FAKE"]
    out, counts = cleaned(pem("PGP PRIVATE KEY BLOCK", lines))
    assert out == "[removed: private key]" and counts == {"private_key": 1}


def test_an_encrypted_key_goes_with_its_headers():
    lines = ["Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF"]
    out, counts = cleaned(pem("RSA PRIVATE KEY", [*lines, "", *KEY_LINES]))
    assert out == "[removed: private key]" and counts == {"private_key": 1}


@pytest.mark.parametrize(
    "text",
    [
        DASHES + "BEGIN PRIVATE KEY" + DASHES + " is how such a block starts",
        "It ends with " + DASHES + "END PRIVATE KEY" + DASHES + ", after the key lines.",
        "The words PRIVATE KEY, then a long word, Supercalifragilistic expialidocious.",
    ],
)
def test_talk_about_key_blocks_is_left_alone(text):
    assert cleaned(text) == (text, {})


def test_words_after_a_key_stay():
    out, counts = cleaned(pem() + "\nThanks for looking at this with me")
    assert out == "[removed: private key]\nThanks for looking at this with me"
    assert counts == {"private_key": 1}


N = 2_000_000


def fit(unit):
    return (unit * (N // len(unit) + 1))[:N]


@pytest.mark.parametrize(
    "text",
    [
        fit(DASHES + "END PRIVATE KEY" + DASHES + "\n"),
        fit(DASHES + "END PRIVATE KEY" + DASHES),
        fit("x " + DASHES + "END PRIVATE KEY" + DASHES + " "),
        fit(DASHES + "BEGIN PRIVATE KEY" + DASHES + "\n"),
        fit(DASHES + "BEGIN PRIVATE KEY" + DASHES + "\n" + DASHES + "END PRIVATE KEY" + DASHES),
        fit("FAKE" * 5 + "\n" + DASHES + "END PRIVATE KEY" + DASHES + "\n"),
        "PRIVATE KEY\n" + fit("FAKE" * 16 + "\n"),
        DASHES + "BEGIN PRIVATE KEY" + DASHES + fit("\nVersion:Version:"),
        DASHES + "BEGIN PRIVATE KEY" + DASHES + fit("\\n> <br>"),
        "PRIVATE KEY " + "FAKE" * 5 + "\\" * N,
    ],
    ids=[
        "ends",
        "ends-joined",
        "ends-between-words",
        "begins",
        "begin-end",
        "line-end",
        "lines",
        "headers",
        "breaks",
        "backslashes",
    ],
)
def test_two_megabytes_of_key_markers_take_linear_time(text):
    # Each END line once looked back over the 16 KB before it: 2 MB of them took minutes. One
    # pass takes well under a second here; the limit leaves room for a loaded machine.
    started = time.perf_counter()
    redaction.redact(text)
    assert time.perf_counter() - started < 10
