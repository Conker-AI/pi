"""Markdown to a flat list of blocks every export format can draw; no HTML, no fetches.

The dashboard reads answers as CommonMark with GitHub tables, strikethrough and task lists, raw
HTML skipped, and links only when they are http, https or mailto. This keeps the same reading:
any other link (javascript:, data:, a citation id) stays its plain label, images stay their alt
text and a link, and raw HTML stays literal text. Lists and quotes become an indent and a marker
on each paragraph, so one simple loop draws them in every format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_TEXT = 400_000
MAX_BLOCKS = 10_000
MAX_DEPTH = 12  # Lists and quotes inside each other.
MAX_COLUMNS = 64
MAX_ROWS = 2_000
MAX_CELLS = 40_000  # A spreadsheet holds this many.
MAX_PAGE_CELLS = 10_000  # Documents, PDFs and slides: a bigger table belongs in a spreadsheet.
MAX_CELL_TEXT = 32_000  # Under the spreadsheet limit of 32,767 characters a cell.
MAX_TABLES = 200
_NESTING = 40  # The parser's own limit; reaching it would drop text, so it is refused first.
BULLETS = ("•", "◦", "▪")


class ExportLimit(ValueError):
    """A fixed reason code; the caller turns it into a plain-word message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    href: str | None = None


@dataclass
class Block:
    kind: str  # heading, paragraph, code, table, rule
    spans: list[Span] = field(default_factory=list)
    level: int = 0  # Heading level 1..6.
    indent: int = 0  # List depth.
    quote: int = 0  # Quote depth.
    marker: str | None = None  # The bullet or number of a list item's first paragraph.
    text: str = ""  # Code text.
    language: str = ""
    header: list[list[Span]] = field(default_factory=list)
    rows: list[list[list[Span]]] = field(default_factory=list)
    align: list[str | None] = field(default_factory=list)


def safe_href(value: str | None) -> str | None:
    """A link worth keeping clickable in a file: http, https or mailto, no credentials."""
    if (
        not value
        or len(value) > 2048
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https", "mailto"):
            return None
        if parts.scheme.lower() != "mailto" and (not parts.hostname or parts.username):
            return None
        _ = parts.port
    except ValueError:
        return None
    return value


def plain(spans: list[Span]) -> str:
    return "".join(span.text for span in spans)


def _inline(token) -> list[Span]:
    spans: list[Span] = []
    bold = italic = strike = 0
    links: list[str | None] = []

    def add(text, code=False, href=None):
        if not text:
            return
        link = href if href is not None else (links[-1] if links else None)
        span = Span(text, bool(bold), bool(italic), code, bool(strike), link)
        last = spans[-1] if spans else None
        if last and (last.bold, last.italic, last.code, last.strike, last.href) == (
            span.bold,
            span.italic,
            span.code,
            span.strike,
            span.href,
        ):
            last.text += text
        else:
            spans.append(span)

    for child in token.children or []:
        kind = child.type
        if kind == "text":
            add(child.content)
        elif kind == "code_inline":
            add(child.content, code=True)
        elif kind in ("softbreak", "hardbreak"):
            add(" " if kind == "softbreak" else "\n")
        elif kind in ("strong_open", "strong_close"):
            bold += 1 if kind.endswith("open") else -1
        elif kind in ("em_open", "em_close"):
            italic += 1 if kind.endswith("open") else -1
        elif kind in ("s_open", "s_close"):
            strike += 1 if kind.endswith("open") else -1
        elif kind == "link_open":
            links.append(safe_href(child.attrs.get("href")))
        elif kind == "link_close":
            if links:
                links.pop()
        elif kind == "image":
            add("Image: " + (child.content or "Untitled"))
            source = safe_href(child.attrs.get("src"))
            if source:
                add(" · ")
                add("Open image", href=source)
        else:
            add(child.content)
    return spans


def _task(spans: list[Span], marker: str) -> str:
    """GitHub task lists: '[ ] ' and '[x] ' at the start of an item become a box."""
    if spans and not spans[0].code and spans[0].text[:4] in ("[ ] ", "[x] ", "[X] "):
        box = "☐" if spans[0].text[1] == " " else "☑"
        spans[0].text = spans[0].text[4:]
        return box
    return marker


def _parser():
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": False, "maxNesting": _NESTING})
    parser.enable(["table", "strikethrough"])
    # Every link reaches the block builder, which keeps only safe ones clickable and leaves
    # the rest as their label, the way the dashboard does.
    parser.validateLink = lambda url: True
    return parser


def from_markdown(text: str) -> list[Block]:
    if len(text) > MAX_TEXT:
        raise ExportLimit("too_long")
    tokens = _parser().parse(text)
    if any(token.level >= _NESTING - 2 for token in tokens):
        raise ExportLimit("too_deep")
    blocks: list[Block] = []
    lists: list[dict] = []  # One entry per open list: ordered, next number.
    pending: list[str | None] = []  # The marker each open item still has to place.
    quote = 0
    cells = 0
    index = 0

    def emit(block: Block):
        if len(blocks) >= MAX_BLOCKS:
            raise ExportLimit("too_many_blocks")
        block.indent, block.quote = len(lists), quote
        blocks.append(block)

    def marker_alone():
        # An item that opens with a list, a table or code, or is empty, still shows its marker.
        if pending and pending[-1] is not None:
            emit(Block("paragraph", marker=pending[-1]))
            pending[-1] = None

    def push(block: Block):
        if block.kind == "paragraph" and pending and pending[-1] is not None:
            block.marker = _task(block.spans, pending[-1])
            pending[-1] = None
        marker_alone()
        emit(block)

    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            marker_alone()
            start = int(token.attrs.get("start", 1) or 1) if kind.startswith("ordered") else 0
            lists.append({"ordered": kind.startswith("ordered"), "next": start})
            if len(lists) + quote > MAX_DEPTH:
                raise ExportLimit("too_deep")
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            current = lists[-1]
            if current["ordered"]:
                marker = f"{current['next']}."
                current["next"] += 1
            else:
                marker = BULLETS[(len(lists) - 1) % len(BULLETS)]
            pending.append(marker)
        elif kind == "list_item_close":
            marker_alone()
            pending.pop()
        elif kind == "blockquote_open":
            quote += 1
            if len(lists) + quote > MAX_DEPTH:
                raise ExportLimit("too_deep")
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "heading_open":
            push(Block("heading", _inline(tokens[index + 1]), level=int(token.tag[1])))
            index += 2
        elif kind == "paragraph_open":
            push(Block("paragraph", _inline(tokens[index + 1])))
            index += 2
        elif kind in ("fence", "code_block"):
            push(Block("code", text=token.content.rstrip("\n"), language=token.info.strip()[:40]))
        elif kind == "hr":
            push(Block("rule"))
        elif kind == "table_open":
            table, index = _table(tokens, index)
            cells += len(table.header) * (len(table.rows) + 1)
            if cells > MAX_CELLS or sum(b.kind == "table" for b in blocks) >= MAX_TABLES:
                raise ExportLimit("table_too_big")
            push(table)
        index += 1
    return blocks


def _table(tokens, index):
    table = Block("table")
    row: list[list[Span]] | None = None
    in_head = False
    while tokens[index].type != "table_close":
        token = tokens[index]
        if token.type == "thead_open":
            in_head = True
        elif token.type == "thead_close":
            in_head = False
        elif token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            if in_head:
                table.header = row
            else:
                table.rows.append(row)
                if len(table.rows) > MAX_ROWS:
                    raise ExportLimit("table_too_big")
        elif token.type in ("th_open", "td_open"):
            if in_head:
                style = token.attrs.get("style", "")
                table.align.append(style.removeprefix("text-align:") or None)
            spans = _inline(tokens[index + 1])
            if len(plain(spans)) > MAX_CELL_TEXT:
                raise ExportLimit("table_too_big")
            row.append(spans)
            index += 1
        index += 1
    if len(table.header) > MAX_COLUMNS:
        raise ExportLimit("table_too_big")
    return table, index


def page_cells(blocks: list[Block]):
    if sum(len(b.header) * (len(b.rows) + 1) for b in blocks if b.kind == "table") > MAX_PAGE_CELLS:
        raise ExportLimit("page_cells")


def from_table(columns: list[str], rows: list[list[str]]) -> list[Block]:
    return [
        Block(
            "table",
            header=[[Span(cell)] for cell in columns],
            rows=[[[Span(cell)] for cell in row] for row in rows],
            align=[None] * len(columns),
        )
    ]


def citation_blocks(citations: list[dict]) -> list[Block]:
    """The sources supplied with an answer, as a short list instead of raw metadata."""
    if not citations:
        return []
    blocks = [
        Block("heading", [Span("Supplied source references")], level=2),
        Block(
            "paragraph",
            [
                Span(
                    "These references were supplied with the source response; "
                    "they were not independently verified."
                )
            ],
        ),
    ]
    for number, item in enumerate(citations, 1):
        label = str(item.get("label") or item.get("id") or "Source")
        href = safe_href(item.get("href"))
        spans = [Span(label, href=href)]
        if href and href != label:
            spans.append(Span(" " + href))
        if item.get("excerpt"):
            spans.append(Span(" — " + str(item["excerpt"]), italic=True))
        blocks.append(Block("paragraph", spans, indent=1, marker=f"{number}."))
    return blocks
