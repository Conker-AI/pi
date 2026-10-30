"""Markdown read the way the dashboard reads it, into blocks every export format draws."""

import pytest

from pi.content import markdown_blocks as m


def kinds(blocks):
    return [b.kind for b in blocks]


def test_structure_maps_headings_inline_lists_quotes_code_rules_and_tables():
    blocks = m.from_markdown(
        "# Title *x*\n"
        "Some **bold**, *italic*, ~~gone~~ and `code`.\nsame paragraph  \nhard break\n\n"
        "3. three\n4. four\n   - inner\n     - [x] done\n   - [ ] todo\n\n"
        "> quoted\n> > twice\n\n"
        "```python\nx = 1\n```\n\n---\n\n"
        "| Left | Mid | Right |\n|:--|:-:|--:|\n| a | b | c |\n"
    )
    assert kinds(blocks) == ["heading", *["paragraph"] * 8, "code", "rule", "table"]
    heading, text = blocks[0], blocks[1]
    assert heading.level == 1 and [(s.text, s.italic) for s in heading.spans] == [
        ("Title ", False),
        ("x", True),
    ]
    assert m.plain(text.spans) == "Some bold, italic, gone and code. same paragraph\nhard break"
    assert [(s.text, s.bold, s.italic, s.strike, s.code) for s in text.spans[1:8:2]] == [
        ("bold", True, False, False, False),
        ("italic", False, True, False, False),
        ("gone", False, False, True, False),
        ("code", False, False, False, True),
    ]
    items = [(b.marker, b.indent, m.plain(b.spans)) for b in blocks[2:7]]
    assert items == [
        ("3.", 1, "three"),
        ("4.", 1, "four"),
        ("◦", 2, "inner"),
        ("☑", 3, "done"),
        ("☐", 2, "todo"),
    ]
    assert [(b.quote, m.plain(b.spans)) for b in blocks[7:9]] == [(1, "quoted"), (2, "twice")]
    assert (blocks[9].text, blocks[9].language) == ("x = 1", "python")
    table = blocks[11]
    assert [m.plain(c) for c in table.header] == ["Left", "Mid", "Right"]
    assert [[m.plain(c) for c in row] for row in table.rows] == [["a", "b", "c"]]
    assert table.align == ["left", "center", "right"]


def test_an_item_that_opens_with_a_list_keeps_its_marker():
    blocks = m.from_markdown("1. - nested first\n2. second\n")
    assert [(b.marker, b.indent, m.plain(b.spans)) for b in blocks] == [
        ("1.", 1, ""),
        ("◦", 2, "nested first"),
        ("2.", 1, "second"),
    ]


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>x</script>",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "citation:source-1",
        "/relative/path",
        "#anchor",
        "https://user:secret@example.com/",
        "http:///no-host",
    ],
)
def test_unsafe_links_stay_plain_labels(link):
    (block,) = m.from_markdown(f"Read [the label]({link}) now.")
    assert m.plain(block.spans) == "Read the label now."
    assert all(span.href is None for span in block.spans)


def test_safe_links_and_images_never_fetch_and_html_stays_literal():
    (block,) = m.from_markdown(
        "[web](https://example.com/a?b=1) [mail](mailto:owner@example.com) "
        "![chart](https://example.com/c.png) ![x](javascript:alert(1)) <b>raw</b> &amp;"
    )
    links = [(s.text, s.href) for s in block.spans if s.href]
    assert links == [
        ("web", "https://example.com/a?b=1"),
        ("mail", "mailto:owner@example.com"),
        ("Open image", "https://example.com/c.png"),
    ]
    assert "Image: chart · Open image" in m.plain(block.spans)
    assert m.plain(block.spans).endswith("Image: x <b>raw</b> &")


@pytest.mark.parametrize(
    "text",
    [
        "".join("  " * level + "- deeper\n" for level in range(300)),
        "- " * 5000 + "x",
        ">" * 5000 + " x",
        "> " * 8 + "- " * 8 + "x",
    ],
    ids=["indented-lists", "inline-lists", "quotes", "quotes-and-lists"],
)
def test_deep_nesting_is_refused_never_truncated(text):
    with pytest.raises(m.ExportLimit) as caught:
        m.from_markdown(text)
    assert caught.value.code == "too_deep"


def test_twelve_levels_still_export():
    blocks = m.from_markdown("".join("  " * level + "- level\n" for level in range(12)))
    assert [b.indent for b in blocks] == list(range(1, 13))


def table(columns, rows, cell="v"):
    head = "|" + "|".join(f"c{i}" for i in range(columns)) + "|\n"
    rule = "|" + "---|" * columns + "\n"
    return head + rule + ("|" + "|".join([cell] * columns) + "|\n") * rows


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("x" * (m.MAX_TEXT + 1), "too_long"),
        (table(2, m.MAX_ROWS + 1), "table_too_big"),
        (table(m.MAX_COLUMNS + 1, 1), "table_too_big"),
        (table(1, 1, "y" * (m.MAX_CELL_TEXT + 1)), "table_too_big"),
        ("\n\n".join([table(40, 1)] * 600), "table_too_big"),
        ("\n\n".join(["p"] * (m.MAX_BLOCKS + 1)), "too_many_blocks"),
    ],
    ids=["text", "rows", "columns", "cell", "cells", "blocks"],
)
def test_size_limits_are_fixed_codes(text, code):
    with pytest.raises(m.ExportLimit) as caught:
        m.from_markdown(text)
    assert caught.value.code == code


def test_page_formats_take_a_smaller_table_than_spreadsheets():
    blocks = m.from_markdown(table(20, 600))
    with pytest.raises(m.ExportLimit) as caught:
        m.page_cells(blocks)
    assert caught.value.code == "page_cells"
    m.page_cells(m.from_markdown(table(20, 400)))


def test_brackets_and_emphasis_runs_parse_as_text():
    (block,) = m.from_markdown("[" * 5000 + "*" * 5000 + "x")
    assert m.plain(block.spans) == "[" * 5000 + "*" * 5000 + "x"


def test_citations_become_a_plain_list_with_safe_links():
    blocks = m.citation_blocks(
        [
            {"id": "a", "label": "Example", "href": "https://example.com/a", "excerpt": "Quoted"},
            {"id": "b", "label": "No link"},
        ]
    )
    assert m.plain(blocks[0].spans) == "Supplied source references"
    assert "not independently verified" in m.plain(blocks[1].spans)
    first, second = blocks[2:]
    assert first.marker == "1." and first.spans[0].href == "https://example.com/a"
    assert m.plain(first.spans) == "Example https://example.com/a — Quoted"
    assert m.plain(second.spans) == "No link" and second.spans[0].href is None
    assert m.citation_blocks([]) == []
