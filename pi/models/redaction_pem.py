"""Private key blocks, for `pi.models.redaction`, found in one forward pass over the text.

A block is a BEGIN line, the key lines after it and its END line. Each half of a block cut in
two is taken too: a BEGIN line with the key lines after it, or key lines with the END line
after them. Key lines may be broken by line breaks written out, by JSON escapes at any depth
(`\\n`, `\\\\n`), by HTML's `<br>` or by spaces, and each may start with a quoting or comment
mark (`> `, `# `, `// `, `| `). Header lines (`Proc-Type:`, `Version:` ...) may follow the
BEGIN line. The search reads each part of the text once, so a text made of nothing but BEGIN or
END lines costs no more than any other. A replacement never starts or ends inside a JSON escape
and never takes a quotation mark, so JSON around a key stays valid.
"""

from __future__ import annotations

import re

MARKER = "[removed: private key]"
_DASHES = "-" * 5
# What may stand between two key lines: a line break written out or escaped at any depth,
# HTML's <br>, spaces, and a quoting or comment mark at the start of the next line.
_BREAK = r"(?:[ \t\r\n]|\\+[nrt]|<br\s*/?>)"
_QUOTE = r"(?:>|\||#|//)"
_SEP = rf"(?:{_BREAK}|{_QUOTE})++"
_HEADER = r"(?:Proc-Type|DEK-Info|Version|Comment|Hash):[^\r\n\"\\]*+"
# One base64 character; a slash may be escaped (`\/`), as some JSON writers do.
_B64 = r"(?:[A-Za-z0-9+]|\\*/)"
_LONG = rf"{_B64}{{20,}}+={{0,2}}"
_LINE = rf"(?:{_LONG}|{_B64}{{1,19}}+={{1,2}}|={_B64}{{4}})"
# Tokens, left to right: a BEGIN or END line, or key lines. Key lines start a word (after a
# space or a mark, or after a JSON escape's n, r or t) with a long line, and take the lines that
# follow; a short unpadded last line (and a PGP checksum line) only when the END line is next.
_SCAN = re.compile(
    rf"{_DASHES}(?P<kind>BEGIN|END) [A-Z0-9 ]{{0,40}}?PRIVATE KEY(?: BLOCK)?{_DASHES}"
    rf"|(?:(?<=\\[nrt])|(?<![A-Za-z0-9+/=\\])){_LONG}(?:{_SEP}{_LINE})*+"
    rf"(?:{_SEP}{_B64}{{1,19}}+(?:{_SEP}={_B64}{{4}})?(?=(?:{_SEP})?{_DASHES}END))?"
)
# Between two tokens of one block: only breaks, marks and header lines.
_GAP = re.compile(rf"(?:{_BREAK}|{_QUOTE}|{_HEADER})*+")


def private_keys(text: str) -> tuple[str, int]:
    """The text with each private key block, or half of one, replaced by the marker, and how
    many were replaced."""
    if "PRIVATE KEY" not in text:
        return text, 0
    spans: list[tuple[int, int]] = []
    block: list | None = None  # [start, end, has key lines]: a BEGIN line and what followed it
    lines: list | None = None  # [start, end]: key lines with no BEGIN line before them
    read = 0
    for token in _SCAN.finditer(text):
        joined = _GAP.fullmatch(text, read, token.start()) is not None
        read = token.end()
        kind = token.group("kind")
        open_ = block if block is not None else lines
        if joined and open_ is not None and kind != "BEGIN":
            if kind == "END":
                spans.append((open_[0], token.end()))
                block = lines = None
            else:
                open_[1] = token.end()
                if block is not None:
                    block[2] = True
            continue
        if block is not None and block[2]:
            spans.append((block[0], block[1]))
        block = [token.start(), token.end(), False] if kind == "BEGIN" else None
        lines = [token.start(), token.end()] if kind is None else None
    if block is not None and block[2]:
        spans.append((block[0], block[1]))
    if not spans:
        return text, 0
    pieces, last = [], 0
    for start, end in spans:
        pieces += [text[last:start], MARKER]
        last = end
    return "".join([*pieces, text[last:]]), len(spans)
