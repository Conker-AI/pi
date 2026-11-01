"""Tests that docs/direct-providers.md and README.md stay in sync with direct provider adapters."""

import re
from pathlib import Path

from pi.models.direct import ADAPTERS
from pi.models.secrets import PROVIDER_KEYS


def _adapter_tuples():
    """Return (key, name, url) tuples from the real ADAPTERS."""
    return [(key, cls.name, cls.url) for key, cls in ADAPTERS]


def _compatible_provider_names():
    """Return the set of provider names that are OpenAI-compatible vendors.
    
    Based on the current table in docs/direct-providers.md and the analysis
    that OpenAI and Anthropic are not in the table while the other five are.
    """
    return {"gemini", "xai", "mistral", "deepseek", "groq"}


def doc_problems(doc_text: str, readme_text: str, adapters: list[tuple[str, str, str]]) -> list[str]:
    """Check for drift between documentation and adapter registration.
    
    Args:
        doc_text: Content of docs/direct-providers.md
        readme_text: Content of README.md  
        adapters: List of (key, name, url) tuples from ADAPTERS
        
    Returns:
        List of human-readable problem descriptions, one per drift.
    """
    problems = []
    
    # Parse the table rows from direct-providers.md
    table_pattern = re.compile(r"^\| `(\w+)` \| `(PI_\w+)` \| `(https://[^`]+)` \|", re.MULTILINE)
    table_rows = {}
    for match in table_pattern.finditer(doc_text):
        provider_id, key_var, url = match.groups()
        table_rows[provider_id] = (key_var, url)
    
    # Build adapter lookups
    adapter_by_name = {name: (key, url) for key, name, url in adapters}
    adapter_keys = {key for key, _, _ in adapters}
    compatible_names = _compatible_provider_names()
    
    # Check 1: Table rows match adapters for compatible providers
    for provider_id, (key_var, url) in table_rows.items():
        if provider_id not in adapter_by_name:
            problems.append(f"Table row for unknown provider '{provider_id}'")
            continue
            
        adapter_key, adapter_url = adapter_by_name[provider_id]
        if key_var != adapter_key:
            problems.append(f"Provider '{provider_id}' has wrong key in table: '{key_var}' != '{adapter_key}'")
        if url != adapter_url:
            problems.append(f"Provider '{provider_id}' has wrong URL in table: '{url}' != '{adapter_url}'")
    
    # Check 2: Compatible providers have table rows
    for provider_id in compatible_names:
        if provider_id not in table_rows:
            problems.append(f"Compatible provider '{provider_id}' missing from table")
    
    # Check 3: All adapter keys appear in docs/direct-providers.md
    for key, _, _ in adapters:
        if key not in doc_text:
            problems.append(f"Adapter key '{key}' missing from docs/direct-providers.md")
    
    # Check 4: All adapter keys appear in README.md
    for key, _, _ in adapters:
        if key not in readme_text:
            problems.append(f"Adapter key '{key}' missing from README.md")
    
    return problems


def test_the_docs_match_the_adapters():
    """Real files should have no drift."""
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "direct-providers.md"
    readme_path = repo_root / "README.md"
    
    doc_text = doc_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    adapters = _adapter_tuples()
    
    problems = doc_problems(doc_text, readme_text, adapters)
    assert problems == [], f"Documentation drift detected: {problems}"


def test_doc_problems_detects_url_changes():
    """Changing a URL should be detected."""
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "direct-providers.md"
    readme_path = repo_root / "README.md"
    
    doc_text = doc_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    adapters = _adapter_tuples()
    
    # Change Gemini URL
    broken_doc = doc_text.replace(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions/v2"
    )
    
    problems = doc_problems(broken_doc, readme_text, adapters)
    assert len(problems) > 0
    assert any("gemini" in problem.lower() for problem in problems)
    assert any("url" in problem.lower() for problem in problems)


def test_doc_problems_detects_deleted_row():
    """Deleting a table row should be detected."""
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "direct-providers.md"
    readme_path = repo_root / "README.md"
    
    doc_text = doc_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    adapters = _adapter_tuples()
    
    # Remove the xAI row
    broken_doc = re.sub(
        r"^\| `xai` \| `PI_XAI_KEY` \| `https://api\.x\.ai/v1/chat/completions` \|\n",
        "",
        doc_text,
        flags=re.MULTILINE
    )
    
    problems = doc_problems(broken_doc, readme_text, adapters)
    assert len(problems) > 0
    assert any("xai" in problem.lower() for problem in problems)


def test_doc_problems_detects_key_rename_in_doc():
    """Renaming a key variable in the doc should be detected."""
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "direct-providers.md"
    readme_path = repo_root / "README.md"
    
    doc_text = doc_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    adapters = _adapter_tuples()
    
    # Rename PI_XAI_KEY to PI_X_KEY in the doc
    broken_doc = doc_text.replace("PI_XAI_KEY", "PI_X_KEY")
    
    problems = doc_problems(broken_doc, readme_text, adapters)
    assert len(problems) > 0
    assert any("xai" in problem.lower() for problem in problems)
    assert any("key" in problem.lower() for problem in problems)


def test_doc_problems_detects_missing_key_in_readme():
    """Removing a key from README should be detected."""
    repo_root = Path(__file__).resolve().parents[1]
    doc_path = repo_root / "docs" / "direct-providers.md"
    readme_path = repo_root / "README.md"
    
    doc_text = doc_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    adapters = _adapter_tuples()
    
    # Remove PI_GEMINI_KEY from README
    broken_readme = readme_text.replace("PI_GEMINI_KEY", "")
    
    problems = doc_problems(doc_text, broken_readme, adapters)
    assert len(problems) > 0
    assert any("PI_GEMINI_KEY" in problem for problem in problems)


def test_every_adapter_key_is_a_known_secret():
    """All ADAPTERS keys must be in pi.models.secrets.PROVIDER_KEYS."""
    adapter_keys = {key for key, _ in ADAPTERS}
    
    # Check that all adapter keys are in PROVIDER_KEYS
    missing_keys = adapter_keys - set(PROVIDER_KEYS)
    assert missing_keys == set(), f"Adapter keys not in PROVIDER_KEYS: {missing_keys}"
    
    # Check that PROVIDER_KEYS doesn't have extra adapter keys (optional sanity check)
    # Note: PROVIDER_KEYS may have additional keys like PI_OPENROUTER_KEY, PI_SPEECH_KEY
    # that aren't in ADAPTERS, so we only check the subset relationship