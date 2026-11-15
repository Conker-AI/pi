"""Prompt templates (R-F8), the fill rules: each value the model gives is checked against its field,
and the text Pi builds is pinned to the dashboard's `compilePrompt`."""

import pytest

from pi.agents import template_fill
from pi.agents.template_starters import STARTERS

# --- checking what the model says ---


def field(kind, **extra):
    spec = {"id": "f", "label": "F", "type": kind}
    return {**spec, **extra}


PICK = ["Table", "Report"]


@pytest.mark.parametrize(
    "spec, value, expected",
    [
        (field("shortText"), "  hello ​", "hello"),
        (field("shortText"), "x" * 500, "x" * template_fill.SHORT_LIMIT),
        (field("shortText"), 5, None),
        (field("shortText"), "   ", None),
        (field("longText"), "a\nb", "a\nb"),
        (field("longText"), ["a"], None),
        (field("number"), "42", "42"),
        (field("number"), 3.5, "3.5"),
        (field("number"), "-7,25", "-7,25"),
        (field("number"), "about ten", None),
        (field("number"), True, None),
        (field("date"), "2026-12-24", "2026-12-24"),
        (field("date"), "2026-02-30", None),
        (field("date"), "tomorrow", None),
        (field("switch"), True, True),
        (field("switch"), False, False),
        (field("switch"), "yes", None),
        (field("list"), [" a ", "a", "", 3, "b"], ["a", "b"]),
        (field("list"), "a, b", None),
        (field("list"), [], None),
        (field("choice", options=PICK), "table", "Table"),
        (field("choice", options=PICK), "Slides", None),
        (field("choice", options=PICK), ["Table"], None),
        (field("choice", options=PICK, multiple=True), ["report", "x", "Report"], ["Report"]),
        (field("choice", options=PICK, multiple=True), "Table", None),
    ],
)
def test_each_value_is_checked_against_its_field(spec, value, expected):
    assert template_fill.check(spec, value) == expected


def test_a_list_is_capped():
    many = [f"item {n}" for n in range(50)]
    assert len(template_fill.check(field("list"), many)) == template_fill.ITEMS_MAX


RESEARCH = {"name": "Research", "description": "", "fields": STARTERS[0]["fields"]}


def test_only_the_templates_own_fields_with_fitting_values_are_kept():
    values = {"subject": "S", "output": "Poster", "do": "not a list", "other": "x"}
    assert template_fill.checked(RESEARCH, values) == {"subject": "S"}


# --- the text Pi builds: the dashboard's compilePrompt, pinned ---


def test_the_text_is_what_the_dashboards_compile_prompt_builds():
    # Worked by hand from dashboard/src/lib/chat/prompt-templates.ts: a heading, then one block
    # per filled field in template order, a blank line between blocks, none at the end; a list is
    # "- " lines, a long text goes under its label, an empty field leaves no trace.
    values = {
        "subject": "  Best laptop  ",
        "do": ["Compare battery life", " ", "Price"],
        "dont": [],
        "output": "Table",
        "context": "I write Python",
    }
    assert template_fill.compile_prompt(RESEARCH, values) == (
        "Research request\n\nSubject:\nBest laptop\n\nDo:\n- Compare battery life\n- Price\n\n"
        "Output: Table\n\nContext:\nI write Python"
    )


def test_a_switch_a_multiple_choice_a_number_and_a_date_are_written_the_same_way():
    template = {
        "name": "Code",
        "description": "",
        "fields": [
            field("switch", id="tests", label="Include tests"),
            field("switch", id="off", label="Off"),
            field("choice", id="tone", label="Tone", options=["A", "B"], multiple=True),
            field("number", id="max", label="Maximum words"),
            field("date", id="by", label="Decide by"),
            field("shortText", id="empty", label="Empty"),
        ],
    }
    values = {
        "tests": True,
        "off": False,
        "tone": ["A", "B"],
        "max": "200",
        "by": "2026-12-24",
        "empty": "  ",
    }
    assert template_fill.compile_prompt(template, values) == (
        "Code request\n\nInclude tests: Yes\n\nTone: A, B\n\nMaximum words: 200\n\n"
        "Decide by: 2026-12-24"
    )


def test_a_template_with_nothing_filled_is_only_its_heading():
    assert template_fill.compile_prompt(RESEARCH, {}) == "Research request"


def test_the_required_fields_still_empty_are_named():
    assert template_fill.missing(RESEARCH, {}) == ["Subject"]
    assert template_fill.missing(RESEARCH, {"subject": "  "}) == ["Subject"]
    assert template_fill.missing(RESEARCH, {"subject": "x"}) == []
    needs = {"name": "N", "description": "", "fields": [field("switch", required=True)]}
    assert template_fill.missing(needs, {"f": False}) == ["F"]
    assert template_fill.missing(needs, {"f": True}) == []
    lists = {"name": "N", "description": "", "fields": [field("list", required=True)]}
    assert template_fill.missing(lists, {"f": []}) == ["F"]
