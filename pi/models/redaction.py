"""Hide secrets from online models: the owner's `privacy.redact` (Settings > Privacy and data).

Every request to an online model leaves through one wrapper, `Redacting`, which `pi.api.startup`
puts around each online adapter: OpenRouter, the direct-key providers, the ChatGPT and Claude
subscriptions, and a decision service that is not on this server's loopback. Ollama and a decision
service on loopback are local and get every request as it is. While the setting is on, the wrapper
sends a copy of the messages (system, owner, assistant, tool results, memories, attachment text) in
which private key blocks, API keys and tokens, password assignments and card numbers are replaced
by a fixed marker ("[removed: key]"). The messages it was given, the stored chat and the model's
answer, with any tool request in it, are never changed.

Precision before recall: each secret needs a known shape (a key header, a vendor prefix with a
digit, a card network's prefix with the Luhn check), so ordinary numbers, dates, phone numbers,
git SHAs and UUIDs stay. A secret without one (a password in a sentence, a token cut between two
messages) is not found. A replacement never takes a quotation mark or half of a JSON escape, so a
tool request or tool result in JSON stays valid JSON.

Off unless the owner turns it on (the registry's default). Fail closed: a setting Pi cannot read
counts as on, and a request Pi could not clean is not sent (a model refusal, so the role moves on
to its fallback). How many of each kind were removed, never what, goes on the call's line in the turn's usage
receipt (`pi.context.research_usage`).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from urllib.parse import urlsplit

from pi.core import owner_settings as owner_settings
from pi.core.provider_types import Message
from pi.models.providers import NotBilled

log = logging.getLogger(__name__)

SETTING = "privacy.redact"
DEFAULT = False  # the registry's default
MARKERS = {
    "private_key": "[removed: private key]",
    "key": "[removed: key]",
    "password": "[removed: password]",
    "card": "[removed: card number]",
}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
UNSENT = "Pi could not hide the secrets in this request, so it was not sent to an online model."

# Space between lines, written out or as a JSON escape.
_SPACE = r"(?:[ \t\r\n]|\\[nrt])+"

# A word starts before each: no word character before it, or the n, r or t of a JSON escape.
_TOKEN = re.compile(
    r"""(?:(?<=\\[nrt])|(?<![A-Za-z0-9_]))
    (?:
        sk-[A-Za-z0-9_-]{20,}                           # OpenAI, Anthropic, OpenRouter, DeepSeek
      | [rs]k_(?:live|test)_[A-Za-z0-9]{16,}            # Stripe
      | gh[oprsu]_[A-Za-z0-9]{30,}                      # GitHub
      | github_pat_[A-Za-z0-9_]{40,}
      | xox[abprs]-[A-Za-z0-9-]{10,}                    # Slack
      | (?:AKIA|ASIA)[A-Z0-9]{16}                       # AWS access key ids
      | AIza[A-Za-z0-9_-]{35}                           # Google
      | eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*   # JSON web tokens
      | [Bb]earer[ \t]+(?P<bearer>[A-Za-z0-9._~+/-]{16,}=*)
    )(?![A-Za-z0-9_-])""",
    re.VERBOSE,
)
# Every token starts with one of these; a text with none of them is not searched for tokens.
_TOKEN_HINTS = (
    *("sk-", "k_live_", "k_test_", "github_pat_", "xox", "AKIA", "ASIA", "AIza", "eyJ", "earer"),
    *("gh" + letter + "_" for letter in "oprsu"),
)

# A secret's name ends right before its separator (`_named`), so password_length=12 or
# PASSWORD_FILE=/run/x are not passwords. Found in an ASCII lower-case copy of the text, which
# keeps every offset; the value is then read from the text itself.
_NAMED_SEPARATOR = re.compile(
    rb"(?:word|wd|phrase|pwd|secret|key|token)[\"'\\ \t]{0,4}(?::|=(?!=))"
)
_KEY_WORDS = ("api", "access", "secret", "private")
_TOKEN_WORDS = ("access", "refresh", "auth", "api", "bearer", "session", "id")
_NAMES = tuple(
    {"password", "passwd", "passphrase", "pwd", "secret"}
    | {
        first + joint + last
        for joint in ("_", "-", "")
        for first, last in (
            *((word, "key") for word in _KEY_WORDS),
            ("client", "secret"),
            *((word, "token") for word in _TOKEN_WORDS),
        )
    }
)
_VALUE = re.compile(
    r"""[ \t]*
    (?:
        "(?P<dq>(?:[^"\\\n]|\\.)+)"                                  # "value"
      | \\"(?P<eq>(?:[^"\\\n]|\\\\|\\[^"\\\n])+)\\"                 # \"value\" in a JSON string
      | '(?P<sq>[^'"\\\n]+)'                                         # 'value'
      | (?P<bare>(?:[^\s"'`\\,;&<>(){}\[\]]|\\u[0-9A-Fa-f]{4}|\\\\)+)  # value
    )""",
    re.VERBOSE,
)
# Values that say nothing: an empty setting, a mask, a reference to somewhere else.
_PLACEHOLDER = re.compile(
    r"(?i:true|false|null|none|nil|undefined)|([*.xX\u2022#-])\1*"
    r"|\$|\$\{[^}]*\}|\$[A-Za-z_]\w*|\{\{.*\}\}|%[A-Za-z_]\w*%|<[^<>]*>"
)
# Code rather than a value: a short word or a dotted.name.
_WORD = re.compile(r"[A-Za-z_]{1,7}|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")

# Starts with the digit, so the search skips everything else, then looks behind it: a word start
# or a JSON escape, never the inside of a longer number, a word or a decimal.
_CARD = re.compile(
    r"\d(?:(?<=\\[nrt]\d)|(?<![0-9A-Za-z_+]\d))(?<!\d[ .,-]\d)"
    r"(?:[ -]?\d){12,18}(?![ -]?\d)(?![A-Za-z_]|[.,]\d)"
)
_NETWORKS = (
    (re.compile(r"4"), {13, 16, 19}),  # Visa
    (re.compile(r"5[1-5]|222[1-9]|22[3-9]\d|2[3-6]\d\d|27[01]\d|2720"), {16}),  # Mastercard
    (re.compile(r"3[47]"), {15}),  # American Express
    (re.compile(r"6011|64[4-9]|65|62"), set(range(16, 20))),  # Discover, UnionPay
    (re.compile(r"35(?:2[89]|[3-8]\d)"), set(range(16, 20))),  # JCB
    (re.compile(r"30[0-5]|3[689]"), set(range(14, 20))),  # Diners Club
)

_DASHES = "-" * 5
_PEM = re.compile(_DASHES + r"(BEGIN|END) [A-Z0-9 ]{0,40}?PRIVATE KEY(?: BLOCK)?" + _DASHES)
_LINE = (
    r"(?:[A-Za-z0-9+/]{20,}={0,2}|[A-Za-z0-9+/]{1,19}={1,2}|=[A-Za-z0-9+/]{4}"
    r"|(?:Proc-Type|DEK-Info|Version|Comment|Hash):[^\r\n\"\\]{0,200})"
)
_NEXT_LINE = re.compile(_SPACE + _LINE)
_ONLY_SPACE = re.compile(r"(?:[ \t\r\n]|\\[nrt])*")
_KEY_LINE = re.compile(_LINE)
_PIECE = re.compile(_SPACE + r"|[^ \t\r\n\\]+|\\")
BEFORE_END = 16_384  # how far back the first half of a key cut in two is looked for

_tally: ContextVar[Counter | None] = ContextVar("redaction_tally", default=None)


def redact(text: str) -> tuple[str, Counter]:
    """The text with every secret it shows replaced by its marker, and how many of each kind."""
    counts: Counter = Counter()
    text = _private_keys(text, counts)
    if any(hint in text for hint in _TOKEN_HINTS):
        text = _TOKEN.sub(lambda match: _token(match, counts), text)
    text = _assignments(text, counts)
    text = _CARD.sub(lambda match: _card(match, counts), text)
    return text, counts


def outgoing(messages) -> tuple[list, Counter]:
    """A redacted copy of the messages; the messages given are never changed."""
    sent, total = [], Counter()
    for message in messages:
        if isinstance(message, Message) and isinstance(message.content, str):
            text, counts = redact(message.content)
            if counts:
                message = replace(message, content=text)
                total.update(counts)
        sent.append(message)
    return sent, total


def enabled(store) -> bool:
    """Whether the owner hides secrets from online models; a setting Pi cannot read counts as on.
    A value that is not true or false counts as unset (the registry's default)."""
    try:
        value = owner_settings.value(store, SETTING, DEFAULT)
    except Exception:
        log.warning("%s could not be read; secrets are hidden from online models", SETTING)
        return True
    return value if isinstance(value, bool) else DEFAULT


@contextmanager
def tally(counts: Counter) -> Iterator[Counter]:
    """Add to `counts` what the online calls made in this block had removed, for their receipt."""
    saved = _tally.set(counts)
    try:
        yield counts
    finally:
        _tally.reset(saved)


class Redacting:
    """An online adapter that is sent only the redacted copy of each request while the owner's
    setting is on; everything else reads through to the adapter."""

    def __init__(self, adapter, store) -> None:
        self._adapter = adapter
        self._store = store

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)  # never recurse before __init__ ran (copy, pickle)
        return getattr(self._adapter, name)

    def complete(self, messages, *, model: str):
        return self._adapter.complete(self._outgoing(messages), model=model)

    def complete_bounded(self, messages, *, model: str, timeout: float):
        sent = self._outgoing(messages)
        return self._adapter.complete_bounded(sent, model=model, timeout=timeout)

    def _outgoing(self, messages):
        if not enabled(self._store):
            return messages
        try:
            sent, counts = outgoing(messages)
        except Exception as exc:
            # Fail closed: a request Pi could not clean is not sent; the role moves on.
            log.warning("redaction failed (%s); the request was not sent", type(exc).__name__)
            raise NotBilled(UNSENT) from None
        seen = _tally.get()
        if counts and seen is not None:
            seen.update(counts)
        return sent


def online(adapter) -> bool:
    """Whether a request to this adapter leaves the server. Only a decision service on this
    server's loopback stays; Ollama, the local model, is never among the adapters wrapped."""
    url = getattr(adapter, "url", None)
    if getattr(adapter, "name", None) == "decisions" and isinstance(url, str):
        return urlsplit(url).hostname not in LOOPBACK
    return True


def wrap(store, hosted, providers: dict) -> tuple:
    """Put OpenRouter and every other online adapter behind the owner's setting."""

    def guarded(adapter):
        return Redacting(adapter, store) if adapter is not None and online(adapter) else adapter

    return guarded(hosted), {name: guarded(adapter) for name, adapter in providers.items()}


def _token(match: re.Match, counts: Counter) -> str:
    value = match.group("bearer") or match.group(0)
    if not any(character.isdigit() for character in value):
        return match.group(0)  # a word, not a key
    counts["key"] += 1
    if match.group("bearer"):
        return match.group(0)[: match.start("bearer") - match.start()] + MARKERS["key"]
    return MARKERS["key"]


def _assignments(text: str, counts: Counter) -> str:
    """Each value given to a secret's name, replaced. The search goes on after every value read,
    kept or not, so no text is read twice."""
    pieces, last, read = [], 0, 0
    for found in _NAMED_SEPARATOR.finditer(text.encode("ascii", "replace").lower()):
        separator = found.end() - 1
        if separator < read or not _named(text, separator):
            continue
        value = _VALUE.match(text, separator + 1)
        if value is None:
            continue
        read = value.end()
        group = next(name for name in ("dq", "eq", "sq", "bare") if value.group(name) is not None)
        if _kept(value.group(group), group, text[read : read + 1]):
            continue
        counts["password"] += 1
        pieces += [text[last : value.start(group)], MARKERS["password"]]
        last = value.end(group)
    return "".join([*pieces, text[last:]]) if pieces else text


def _kept(value: str, form: str, after: str) -> bool:
    """A value that is no secret: a marker, a placeholder, a quote that closes a JSON string
    (`"password:", "next"`), or code (a short word, a dotted.name, a call, an index)."""
    return bool(
        value.startswith("[removed: ")
        or _PLACEHOLDER.fullmatch(value)
        or (form == "dq" and value[0] in " \t,:]}")
        or (form == "bare" and (_WORD.fullmatch(value) or after in ("(", "[")))
    )


def _named(text: str, separator: int) -> bool:
    """Whether a secret's name ends right before the separator, past spaces and the quote that
    closes a quoted name (`"password": `, or `\\"password\\": ` inside a JSON string)."""
    end = separator
    while end and text[end - 1] in " \t":
        end -= 1
    if end and text[end - 1] in "\"'":
        end -= 2 if end > 1 and text[end - 2] == "\\" else 1
    return text[max(0, end - 16) : end].lower().endswith(_NAMES)


def _card(match: re.Match, counts: Counter) -> str:
    raw = match.group(0)
    digits = re.sub(r"[ -]", "", raw)
    groups = re.split(r"[ -]", raw)
    grouped = len(groups) == 1 or (
        len(set(re.findall(r"[ -]", raw))) == 1
        and len(groups[0]) == 4
        and all(len(group) >= 3 for group in groups)
    )
    network = any(prefix.match(digits) and len(digits) in sizes for prefix, sizes in _NETWORKS)
    if not (grouped and network and _luhn(digits)):
        return raw
    counts["card"] += 1
    return MARKERS["card"]


def _luhn(digits: str) -> bool:
    total = 0
    for position, character in enumerate(reversed(digits)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _private_keys(text: str, counts: Counter) -> str:
    """Private key blocks, and each half of one cut in two: a header with the key lines after it,
    or key lines with the end line after them."""
    if "PRIVATE KEY" not in text:
        return text
    marks = list(_PEM.finditer(text))
    spans: list[tuple[int, int]] = []
    done = 0
    for index, mark in enumerate(marks):
        if mark.start() < done:
            continue
        if mark.group(1) == "BEGIN":
            end = mark.end()
            while found := _NEXT_LINE.match(text, end):
                end = found.end()
            after = marks[index + 1] if index + 1 < len(marks) else None
            if (
                after is not None
                and after.group(1) == "END"
                and _ONLY_SPACE.fullmatch(text, end, after.start())
            ):
                end = after.end()
            if end > mark.end():
                spans.append((mark.start(), end))
                done = end
        else:
            start = _lines_before(text, mark.start(), done)
            if start < mark.start():
                spans.append((start, mark.end()))
                done = mark.end()
    if not spans:
        return text
    counts["private_key"] += len(spans)
    pieces, last = [], 0
    for start, end in spans:
        pieces += [text[last:start], MARKERS["private_key"]]
        last = end
    return "".join([*pieces, text[last:]])


def _lines_before(text: str, end: int, floor: int) -> int:
    """Where the key lines just before `end` start; `end` when there are none."""
    start = end
    for piece in reversed(list(_PIECE.finditer(text, max(floor, end - BEFORE_END), end))):
        if _ONLY_SPACE.fullmatch(piece.group()):
            continue
        if not _KEY_LINE.fullmatch(piece.group()):
            break
        start = piece.start()
    return start
