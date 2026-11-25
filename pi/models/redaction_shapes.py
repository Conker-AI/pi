"""Keys, tokens, passwords and card numbers, for `pi.models.redaction`.

Each needs a known shape: a vendor prefix (and a digit, so `sk-learn` stays), a secret's name
right before `:` or `=`, or a card network's prefix and length that pass the Luhn check. A
replacement never takes a quotation mark or half of a JSON escape: a bare value never starts on
a backslash, and a quoted value goes whole, read to its own closing quote at any depth of JSON
written inside JSON strings (`"x"`, `\\"x\\"`, `\\\\\\"x\\\\\\"`). Where a marker stands for
an unquoted JSON value (`{"password": 12345678}`, `[4111111111111111]`) it is quoted at that
value's depth, so JSON stays JSON at any depth.
"""

from __future__ import annotations

import re
from collections import Counter

MARKERS = {
    "key": "[removed: key]",
    "password": "[removed: password]",
    "card": "[removed: card number]",
}

# A word starts before each: no word character or dash before it (so a run of `eyJ-eyJ-` starts
# one token, not one at each dash), or the n, r or t of a JSON escape. The lines after a key that
# a wrap may have cut are read too, and `_wrapped` says which of them are the key's.
_TOKEN = re.compile(
    r"""(?:(?<=\\[nrt])|(?<![A-Za-z0-9_-]))
    (?P<token>
        sk-[A-Za-z0-9_-]{20,}+                          # OpenAI, Anthropic, OpenRouter, DeepSeek
      | [rs]k_(?:live|test)_[A-Za-z0-9]{16,}+           # Stripe
      | gh[oprsu]_[A-Za-z0-9]{30,}+                     # GitHub
      | github_pat_[A-Za-z0-9_]{40,}+
      | xox[abprs]-[A-Za-z0-9-]{10,}+                   # Slack
      | (?:AKIA|ASIA)[A-Z0-9]{16}                       # AWS access key ids
      | AIza[A-Za-z0-9_-]{35}                           # Google
      | hf_[A-Za-z0-9]{30,}+                            # Hugging Face
      | npm_[A-Za-z0-9]{36,}+                           # npm
      | eyJ[A-Za-z0-9_-]{8,}+\.eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]*+   # JSON web tokens
      | [Bb]earer[ \t]+(?P<bearer>[A-Za-z0-9._~+/-]{16,}=*)
    )(?![A-Za-z0-9_-])
    (?P<wrapped>(?:(?:\r?\n|\r|\\++[nr])[A-Za-z0-9.~+/]{8,}+(?=[\s"'\\]|$))*+)""",
    re.VERBOSE,
)
# Every token starts with one of these; a text with none of them is not searched for tokens.
_TOKEN_HINTS = (
    *("sk-", "k_live_", "k_test_", "github_pat_", "xox", "AKIA", "ASIA", "AIza", "eyJ", "earer"),
    *("hf_", "npm_", *("gh" + letter + "_" for letter in "oprsu")),
)
_WRAPPED_LINE = re.compile(r"(\r?\n|\r|\\+[nr])([A-Za-z0-9.~+/]+)")
_WRAP_WIDTH = 60  # the narrowest line a wrap cuts
_LINE_BREAK = re.compile(r"[\r\n]|\\[nr]")
# What a line that continues a key may hold: the key's own characters but = : _ and -.
_BEARER_TAIL = re.compile(r"[A-Za-z0-9.~+/]+")
_JWT_TAIL = re.compile(r"[A-Za-z0-9.]+")
_KEY_TAIL = re.compile(r"[A-Za-z0-9]+")

# A secret's name ends right before its separator (`_name`), so password_length=12 or
# PASSWORD_FILE=/run/x are not secrets. Found in an ASCII lower-case copy of the text, which
# keeps every offset; the value is then read from the text itself. Between the name and the
# separator may stand the quote that closes a quoted name, escaped at any depth of JSON strings,
# and spaces or Markdown's emphasis (`**Password**:`; `**Password:**` closes after it).
_NAMED_SEPARATOR = re.compile(
    rb"(?:word|wd|phrase|pass|secret|key|token)[ \t*]{0,3}(?:\\*+[\"'][ \t*]{0,3})?(?::|=(?!=))"
)
_EMPHASIS = re.compile(r"\*{1,3}+(?=[ \t])")
# Any value after these is a password, unless it is a placeholder or code (`_kept`).
_PASSWORD_NAMES = ("password", "passwd", "passphrase", "pwd", "secret", "_pass", "-pass")
# After these, a value that looks like a key: 8 characters with a digit, or 32 of anything.
_KEY_NAMES = (
    *("_key", "-key", "_token", "-token"),
    *(word + "key" for word in ("api", "access", "secret", "private")),
    *(word + "token" for word in ("access", "refresh", "auth", "api", "bearer", "session", "id")),
)
# A value: quoted (its quote escaped by any number of backslashes, at any depth of JSON strings;
# `_closing` finds its end), in single quotes or backticks, or bare. A bare value starts on no
# backslash and takes only whole escapes: \u00e9, and a run of escaped backslashes that is not
# the start of a line break or a quote.
_VALUE = re.compile(
    r"""[ \t]*+
    (?:
        (?P<dq>\\*+)"                                                # "value", \"value\" ...
      | '(?P<sq>[^'"\\\n]++)'                                        # 'value'
      | `(?P<bq>[^`\n]++)`                                           # `value`
      | (?P<bare>[^\s"'`\\,;&<>(){}\[\]]++
          (?:[^\s"'`\\,;&<>(){}\[\]]++|\\u[0-9A-Fa-f]{4}|(?:\\\\)++(?![\\"'nrtubf/]))*+)
    )""",
    re.VERBOSE,
)
# Where a double-quoted value may end: a whole run of backslashes with its quote, or a line break.
_QUOTE_RUN = re.compile(r'(?<!\\)\\*+"|\n')
# Values that say nothing: a boolean or none, a type, a mask, a reference to somewhere else.
_PLACEHOLDER = re.compile(
    r"(?i:true|false|null|none|nil|undefined|changeme|changeit"
    r"|str|string|bytes|int|bool|boolean|any|object|char|text|varchar|secretstr)"
    r"|([*.xX\u2022#-])\1*|\$|\$\{[^}]*\}|\$[A-Za-z_]\w*|\{\{.*\}\}|%[A-Za-z_]\w*%|<[^<>]*>"
)
# Where a secret is kept rather than the secret: settings.OPENAI_API_KEY, self.password.
_REFERENCE = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.(?:[A-Z][A-Z0-9_]*|\w*(?i:pass|pwd|secret|key|token)\w*)"
)
_PATH = re.compile(r"/|[A-Za-z]:[\\/]")  # an absolute path
# The quote of a JSON member's name, with the backslashes that escape it, right before `: `.
_MEMBER = re.compile(r"(\\*[\"'])[ \t]*:[ \t]*\Z")
# What stands right before a JSON value (a member's colon, an array's bracket or a comma, then
# spaces and line breaks escaped at any depth), and what follows a string's closing quote.
_BEFORE_VALUE = re.compile(r"([\[,:])(?:[ \t\r\n]|\\++[nrt])*+\Z")
_AFTER_STRING = re.compile(r"(?:[ \t\r\n]|\\++[nrt])*+[,:\]}]")

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


def tokens(text: str, counts: Counter) -> str:
    if not any(hint in text for hint in _TOKEN_HINTS):
        return text
    return _TOKEN.sub(lambda match: _token(match, counts), text)


def assignments(text: str, counts: Counter) -> str:
    """Each value given to a secret's name, replaced. The search goes on after every value read,
    kept or not, so no text is read twice."""
    pieces, last, read = [], 0, 0
    for found in _NAMED_SEPARATOR.finditer(text.encode("ascii", "replace").lower()):
        separator = found.end() - 1
        if separator < read or (named := _name(text, separator)) is None:
            continue
        value = _value(text, separator + 1)
        if value is None:
            continue
        form, start, end, read = value
        (kind, name), secret = named, text[start:end]
        if _kept(secret, form, text[read : read + 1], kind):
            continue
        if name.endswith("pwd") and _PATH.match(text, start):
            continue  # pwd: /home/user is a directory, not a password
        counts[kind] += 1
        quote = _member_quote(text, start) if form == "bare" and text[separator] == ":" else ""
        pieces += [text[last:start], quote, MARKERS[kind], quote]
        last = end
    return "".join([*pieces, text[last:]]) if pieces else text


def cards(text: str, counts: Counter) -> str:
    quotes = _Quotes(text)
    return _CARD.sub(lambda match: _card(match, counts, quotes), text)


def _token(match: re.Match, counts: Counter) -> str:
    token = match.group("bearer") or match.group("token")
    if not (_digit(token) or (token.startswith(("hf_", "npm_")) and _mixed(token))):
        return match.group(0)  # a word, not a key
    counts["key"] += 1
    kept = (
        match.group("token")[: match.start("bearer") - match.start()]
        if match.group("bearer")
        else ""
    )
    return kept + MARKERS["key"] + _wrapped(match)


def _wrapped(match: re.Match) -> str:
    """What is left of the lines after a key once the rest of the key is taken. A line is the
    key's only when the line before it is as wide as a wrap cuts (60 characters) and the line
    could be more of the same key: nothing but the key's characters, and random (a digit, or
    both cases). The first line that is not, and all after it, stay."""
    lines = match.group("wrapped")
    if not lines:
        return ""
    token, end = match.group("token"), match.end("token")
    alphabet = (
        _BEARER_TAIL if match.group("bearer") else _JWT_TAIL if token[:3] == "eyJ" else _KEY_TAIL
    )
    # The key's own line: no line break in the 60 characters up to its end.
    wide = end >= _WRAP_WIDTH and _LINE_BREAK.search(match.string, end - _WRAP_WIDTH, end) is None
    for line in _WRAPPED_LINE.finditer(lines):
        rest = line.group(2)
        if not (wide and alphabet.fullmatch(rest) and (_digit(rest) or _mixed(rest))):
            return lines[line.start() :]
        wide = len(rest) >= _WRAP_WIDTH
    return ""


def _digit(value: str) -> bool:
    return any(character.isdigit() for character in value)


def _mixed(value: str) -> bool:
    return value.lower() != value != value.upper()


def _name(text: str, separator: int) -> tuple[str, str] | None:
    """The kind of secret named right before the separator ("password" or "key") and the end of
    its name, past spaces, Markdown's emphasis and the quote that closes a quoted name
    (`"password": `, or `\\"password\\": ` inside a JSON string)."""
    end = separator
    while end and text[end - 1] in " \t*":
        end -= 1
    if end and text[end - 1] in "\"'":
        end -= 1
        while end and text[end - 1] == "\\":
            end -= 1
    name = text[max(0, end - 16) : end].lower()
    if name.endswith(_PASSWORD_NAMES):
        return "password", name
    if name.endswith(_KEY_NAMES):
        return "key", name
    return None


def _value(text: str, at: int) -> tuple[str, int, int, int] | None:
    """The value given at `at`, right after a separator, past Markdown's emphasis: its form, the
    start and end of its text, and where reading may go on. None when there is none there."""
    emphasis = _EMPHASIS.match(text, at)
    found = _VALUE.match(text, emphasis.end() if emphasis else at)
    if found is None:
        return None
    form = found.lastgroup or "bare"  # each form is one named group: the one that matched
    if form != "dq":
        return form, found.start(form), found.end(form), found.end()
    escaped = _depth(len(found.group("dq")))
    start = found.end()
    end = _closing(text, start, escaped)
    if end is None or end == start:
        return None
    return form, start, end, end + escaped + 1


def _closing(text: str, start: int, escaped: int) -> int | None:
    """Where a value whose opening quote is escaped by `escaped` backslashes ends: before the
    first quote escaped the same way. A quote escaped one level deeper is part of the value; a
    line break, or a quote that would close a string around the value, leaves it unclosed."""
    period = 2 * escaped + 2
    for found in _QUOTE_RUN.finditer(text, start):
        if found.group() == "\n":
            return None
        backslashes = len(found.group()) - 1
        if backslashes % period == 2 * escaped + 1:
            continue  # a quote inside the value
        return found.end() - 1 - escaped if backslashes % period == escaped else None
    return None


def _depth(backslashes: int) -> int:
    """How many of the backslashes before a quote escape it as a JSON string delimiter: the
    quote of a string inside `d` JSON strings has 2**d - 1, and any more before it are escaped
    backslashes (pairs, at that depth)."""
    ones = 0
    while backslashes & 1:
        backslashes >>= 1
        ones += 1
    return (1 << ones) - 1


def _kept(value: str, form: str, after: str, kind: str) -> bool:
    """A value that is no secret: a marker, a placeholder, a quote that closes a JSON string
    (`"password:", "next"`), code (a call, an index, where the secret is kept), or after a key's
    name a value too plain to be a key."""
    if value.startswith("[removed: ") or _PLACEHOLDER.fullmatch(value):
        return True
    if form == "dq" and value[0] in " \t,:]}":
        return True
    if form == "bare" and (after in ("(", "[") or _REFERENCE.fullmatch(value)):
        return True
    return kind == "key" and len(value) < 32 and not (len(value) >= 8 and _digit(value))


def _member_quote(text: str, start: int) -> str:
    """The quote, with its escaping backslashes, of the JSON member whose unquoted value starts at
    `start`, so the marker put there is a string at the same depth; "" when there is none."""
    found = _MEMBER.search(text, max(0, start - 40), start)
    if found is None:
        return ""
    quote = found.group(1)
    return "\\" * _depth(len(quote) - 1) + quote[-1]


class _Quotes:
    """The quote a JSON value would be written with at each place asked about, in order, found
    from the double quote nearest before it. Each part of the text is read once."""

    def __init__(self, text: str) -> None:
        self.text, self.read, self.quote = text, 0, '"'

    def at(self, place: int) -> str:
        found = self.text.rfind('"', self.read, place)
        self.read = place
        if found >= 0:
            run = found
            while run and self.text[run - 1] == "\\":
                run -= 1
            escaped = _depth(found - run)
            # After a string that closed, a value is at that string's depth; inside a string
            # that just opened (JSON written in a string, `"[4111...]"`), one deeper.
            if _AFTER_STRING.match(self.text, found + 1) is None:
                escaped = 2 * escaped + 1
            self.quote = "\\" * escaped + '"'
        return self.quote


def _value_quote(text: str, start: int, quotes: _Quotes) -> str:
    """The quote for a marker that stands for an unquoted JSON value at `start`: after a member's
    colon that member's, after `[` or `,` the depth of the strings before it; "" elsewhere."""
    before = _BEFORE_VALUE.search(text, max(0, start - 80), start)
    if before is None:
        return ""
    if before.group(1) == ":":
        return _member_quote(text, before.start() + 1)
    return quotes.at(start)


def _card(match: re.Match, counts: Counter, quotes: _Quotes) -> str:
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
    quote = _value_quote(match.string, match.start(), quotes)
    return quote + MARKERS["card"] + quote


def _luhn(digits: str) -> bool:
    total = 0
    for position, character in enumerate(reversed(digits)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0
