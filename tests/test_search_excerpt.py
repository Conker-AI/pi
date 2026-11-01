"""Unit tests for the search excerpt cutter."""

import pytest

from pi.conversation.search_excerpt import excerpt


class TestExcerptCases:
    """Test specific cases for the excerpt function."""

    @pytest.mark.parametrize(
        "text, query, kwargs, expected",
        [
            # Case with ß that folds to ss (length change)
            ("ßß needle tail", "needle", {"width": 6, "leading": 0}, "needle"),
            # Case insensitive query with leading context
            ("ßß needle tail", "NEEDLE", {"width": 6, "leading": 2}, "ß need"),
            # Non-ASCII case folding without length change
            ("ÄÖ needle", "äö n", {"width": 4, "leading": 0}, "ÄÖ n"),
            # İ folds to two code points
            ("İİ needle", "needle", {"width": 6, "leading": 0}, "needle"),
            # Match at position 0 with large leading
            ("hello world", "hello", {"width": 5, "leading": 80}, "hello"),
            # No match falls back to beginning
            ("hello world", "zzz", {"width": 5, "leading": 2}, "hello"),
            # ASCII text with leading context
            ("0123456789 needle end", "needle", {"width": 8, "leading": 3}, "89 needl"),
            # Empty text
            ("", "x", {}, ""),
            # Empty query matches at 0
            ("abc", "", {}, "abc"),
        ],
    )
    def test_excerpt_cases(self, text, query, kwargs, expected):
        """Test specific excerpt cases with various parameters."""
        result = excerpt(text, query, **kwargs)
        assert result == expected


def test_default_window():
    """Test default window size and positioning."""
    text = "a" * 1000 + "needle" + "b" * 1000
    result = excerpt(text, "needle")
    assert len(result) == 400
    assert "needle" in result
    assert result.index("needle") == 80


def test_a_match_near_the_start_gets_less_leading_context():
    """Test that matches near start get less leading context."""
    text = "x" * 500 + "needle"
    result = excerpt(text, "needle")
    assert len(result) == 86  # start 420 = 500 - 80, then to end
    assert result.endswith("needle")


def test_the_excerpt_always_contains_the_match_when_there_is_one():
    """Test that excerpts always contain the match when there is one."""
    test_cases = [
        # Cases where folding does not change lengths
        ("ÄÖ needle", "äö n", {"width": 10, "leading": 2}),
        ("abc needle def", "needle", {"width": 10, "leading": 2}),
        # Cases with length changes
        ("ßß needle tail", "needle", {"width": 10, "leading": 2}),
        ("İİ needle", "needle", {"width": 10, "leading": 2}),
    ]

    for text, query, kwargs in test_cases:
        result = excerpt(text, query, **kwargs)

        # For cases where folding doesn't change lengths, check casefolded match
        if text in ["ÄÖ needle", "abc needle def"]:
            assert query.casefold() in result.casefold()
        # For ß and İ cases, check that "needle" is literally in result
        else:
            assert "needle" in result


def test_ascii_and_unicode_paths_agree_when_no_length_changes():
    """Test that ASCII and Unicode paths agree when no length changes."""
    ascii_text = "abc needle def"
    unicode_text = "abç needle def"

    ascii_result = excerpt(ascii_text, "needle", width=6, leading=2)
    unicode_result = excerpt(unicode_text, "needle", width=6, leading=2)

    # Both should have same length and similar structure
    assert len(ascii_result) == len(unicode_result)
    assert ascii_result == "c need"
    assert unicode_result == "ç need"
