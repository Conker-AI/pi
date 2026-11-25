"""Keys, tokens, passwords and card numbers, for `pi.models.redaction`.

Each needs a known shape: a vendor prefix (and a digit, so `sk-learn` stays), a secret's name
right before `:` or `=`, or a card network's prefix and length that pass the Luhn check. A
replacement never takes a quotation mark or half of a JSON escape; where it stands for an
unquoted JSON value (`{"password": 12345678}`) it is quoted the way the member's name is, so
JSON stays JSON at any depth.
"""

from __future__ import annotations

import re
from collections import Counter

MARKERS = {
    "key": "[removed: key]",
    "password": "[removed: password]",
    "card": "[removed: card number]",
}

# A word starts before each: no word character before it, or the n, r or t of a JSON escape. A
# key cut by a line wrap takes the random-looking lines right after it too (`_wrapped`).
_TOKEN = re.compile(
    r"""(?:(?<=\\[nrt])|(?<![A-Za-z0-9_]))
    (?P<token>
        sk-[A-Za-z0-9_-]{20,}                           # OpenAI, Anthropic, OpenRouter, DeepSeek
      | [rs]k_(?:live|test)_[A-Za-z0-9]{16,}            # Stripe
      | gh[oprsu]_[A-Za-z0-9]{30,}                      # GitHub
      | github_pat_[A-Za-z0-9_]{40,}
      | xox[abprs]-[A-Za-z0-9-]{10,}                    # Slack
      | (?:AKIA|ASIA)[A-Z0-9]{16}                       # AWS access key ids
      | AIza[A-Za-z0-9_-]{35}                           # Google
      | hf_[A-Za-z0-9]{30,}                             # Hugging Face
      | npm_[A-Za-z0-9]{36,}                            # npm
      | eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*   # JSON web tokens
      | [Bb]earer[ \t]+(?P<bearer>[A-Za-z0-9._~+/-]{16,}=*)
    )(?![A-Za-z0-9_-])
    (?P<wrapped>(?:(?:\r?\n|\r|\\+[nr])[A-Za-z0-9_+/=-]{8,}(?=[\s"'\\]|$))*)""",
    re.VERBOSE,
)
# Every token starts with one of these; a text with none of them is not searched for tokens.
_TOKEN_HINTS = (
    *("sk-", "k_live_", "k_test_", "github_pat_", "xox", "AKIA", "ASIA", "AIza", "eyJ", "earer"),
    *("hf_", "npm_", *("gh" + letter + "_" for letter in "oprsu")),
)
_WRAPPED_LINE = re.compile(r"(\r?\n|\r|\\+[nr])([A-Za-z0-9_+/=-]+)")

# A secret's name ends right before its separator (`_name`), so password_length=12 or
# PASSWORD_FILE=/run/x are not secrets. Found in an ASCII lower-case copy of the text, which
# keeps every offset; the value is then read from the text itself.
_NAMED_SEPARATOR = re.compile(
    rb"(?:word|wd|phrase|pass|secret|key|token)[\"'\\ \t]{0,4}(?::|=(?!=))"
)
# Any value after these is a password, unless it is a placeholder or code (`_kept`).
_PASSWORD_NAMES = ("password", "passwd", "passphrase", "pwd", "secret", "_pass", "-pass")
# After these, a value that looks like a key: 8 characters with a digit, or 32 of anything.
_KEY_NAMES = (
    *("_key", "-key", "_token", "-token"),
    *(word + "key" for word in ("api", "access", "secret", "private")),
    *(word + "token" for word in ("access", "refresh", "auth", "api", "bearer", "session", "id")),
)
_VALUE = re.compile(
    r"""[ \t]*
    (?:
        "(?P<dq>(?:[^"\\\n]++|\\.)+)"                                # "value"
      | \\"(?P<eq>(?:[^"\\\n]++|\\\\|\\[^"\\\n])+)\\"               # \"value\" in a JSON string
      | '(?P<sq>[^'"\\\n]++)'                                        # 'value'
      | (?P<bare>(?:[^\s"'`\\,;&<>(){}\[\]]++|\\u[0-9A-Fa-f]{4}|\\\\)+)  # value
    )""",
    re.VERBOSE,
)
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
        value = _VALUE.match(text, separator + 1)
        if value is None:
            continue
        read = value.end()
        group = value.lastgroup or "bare"  # each form is one named group: the one that matched
        (kind, name), start = named, value.start(group)
        if _kept(value.group(group), group, text[read : read + 1], kind):
            continue
        if name.endswith("pwd") and _PATH.match(text, start):
            continue  # pwd: /home/user is a directory, not a password
        counts[kind] += 1
        bare_member = group == "bare" and text[separator] == ":"
        quote = _member_quote(text, start) if bare_member else ""
        pieces += [text[last:start], quote, MARKERS[kind], quote]
        last = value.end(group)
    return "".join([*pieces, text[last:]]) if pieces else text


def cards(text: str, counts: Counter) -> str:
    return _CARD.sub(lambda match: _card(match, counts), text)


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
    return kept + MARKERS["key"] + _wrapped(match.group("wrapped"))


def _wrapped(lines: str) -> str:
    """What is left of the lines after a key once the rest of the key is taken: each line that
    looks random (a digit, or both cases), up to the first that does not."""
    for line in _WRAPPED_LINE.finditer(lines):
        if not (_digit(line.group(2)) or _mixed(line.group(2))):
            return lines[line.start() :]
    return ""


def _digit(value: str) -> bool:
    return any(character.isdigit() for character in value)


def _mixed(value: str) -> bool:
    return value.lower() != value != value.upper()


def _name(text: str, separator: int) -> tuple[str, str] | None:
    """The kind of secret named right before the separator ("password" or "key") and the end of
    its name, past spaces and the quote that closes a quoted name (`"password": `, or
    `\\"password\\": ` inside a JSON string)."""
    end = separator
    while end and text[end - 1] in " \t":
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
    return quote if len(quote) % 2 == 0 else quote[-1]  # an even run of backslashes is content


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
    quote = _member_quote(match.string, match.start())
    return quote + MARKERS["card"] + quote


def _luhn(digits: str) -> bool:
    total = 0
    for position, character in enumerate(reversed(digits)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0
