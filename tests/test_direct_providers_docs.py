"""docs/direct-providers.md and README.md stay in step with the direct provider adapters."""

import re
from pathlib import Path

import pytest

from pi.models.direct import ADAPTERS, OpenAICompatibleProvider
from pi.models.secrets import PROVIDER_KEYS

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(r"^\| `(\w+)` \| `(PI_\w+)` \| `(https://[^`]+)` \|", re.MULTILINE)


def adapter_tuples():
    """(key, name, url) for every real adapter."""
    return [(key, cls.name, cls.url) for key, cls in ADAPTERS]


def compatible_names():
    """Vendors that speak the OpenAI wire format through OpenAICompatibleProvider.

    Those are the ones the table lists; OpenAI and Anthropic have their own adapters.
    """
    return {cls.name for _, cls in ADAPTERS if issubclass(cls, OpenAICompatibleProvider)}


def doc_problems(doc_text, readme_text, adapters, compatible):
    """One short string per drift between the docs and the adapter registry."""
    problems = []
    by_name = {name: (key, url) for key, name, url in adapters}
    rows = {}
    for provider, key, url in ROW.findall(doc_text):
        rows[provider] = (key, url)
        if provider not in by_name:
            problems.append(f"table row for '{provider}' is not an adapter")
            continue
        adapter_key, adapter_url = by_name[provider]
        if key != adapter_key:
            problems.append(f"'{provider}' key in table is {key}, adapter uses {adapter_key}")
        if url != adapter_url:
            problems.append(f"'{provider}' url in table is {url}, adapter uses {adapter_url}")
    for provider in sorted(compatible):
        if provider not in rows:
            problems.append(f"compatible adapter '{provider}' has no table row")
    for key, _, _ in adapters:
        if key not in doc_text:
            problems.append(f"{key} is missing from docs/direct-providers.md")
        if key not in readme_text:
            problems.append(f"{key} is missing from README.md")
    return problems


@pytest.fixture
def texts():
    doc = (ROOT / "docs" / "direct-providers.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return doc, readme


def check(doc, readme):
    return doc_problems(doc, readme, adapter_tuples(), compatible_names())


def test_the_compatible_vendors_are_the_five_in_the_table():
    assert compatible_names() == {"gemini", "xai", "mistral", "deepseek", "groq"}


def test_the_docs_match_the_adapters(texts):
    assert check(*texts) == []


def test_checker_flags_a_changed_url(texts):
    doc, readme = texts
    url = "https://api.x.ai/v1/chat/completions"
    assert url in doc
    problems = check(doc.replace(url, url.replace("/v1/", "/v2/")), readme)
    assert len(problems) == 1
    assert "'xai'" in problems[0] and "url" in problems[0]


def test_checker_flags_a_deleted_row(texts):
    doc, readme = texts
    lines = doc.splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("| `xai` |")]
    assert len(kept) == len(lines) - 1
    problems = check("".join(kept), readme)
    assert any("'xai'" in p and "no table row" in p for p in problems)


def test_checker_flags_a_renamed_key_in_the_doc(texts):
    doc, readme = texts
    problems = check(doc.replace("PI_XAI_KEY", "PI_X_KEY"), readme)
    assert any("'xai'" in p and "PI_X_KEY" in p for p in problems)
    assert any("PI_XAI_KEY is missing from docs" in p for p in problems)


def test_checker_flags_a_key_removed_from_the_readme(texts):
    doc, readme = texts
    assert "PI_GEMINI_KEY" in readme
    problems = check(doc, readme.replace("PI_GEMINI_KEY", ""))
    assert problems == ["PI_GEMINI_KEY is missing from README.md"]


def test_checker_flags_a_row_for_a_provider_that_is_not_an_adapter(texts):
    doc, readme = texts
    extra = "| `acme` | `PI_ACME_KEY` | `https://api.acme.example/v1` | x | y |\n"
    problems = check(doc + extra, readme)
    assert any("'acme'" in p and "not an adapter" in p for p in problems)


def test_checker_works_on_made_up_data():
    adapters = [("PI_A_KEY", "a", "https://a.example/v1")]
    doc = "| `a` | `PI_A_KEY` | `https://a.example/v1` | x |\n"
    assert doc_problems(doc, "PI_A_KEY", adapters, {"a"}) == []
    assert doc_problems("PI_A_KEY", "PI_A_KEY", adapters, {"a"}) == [
        "compatible adapter 'a' has no table row"
    ]


def test_every_adapter_key_is_a_known_secret():
    assert {key for key, _ in ADAPTERS} <= set(PROVIDER_KEYS)
