"""Invisible code points removed from text on its way to a model.

The same rule lives in ToolGate (toolgate/core/untrusted_text.py, its web and MCP text);
change both together. Their tests pin the same fingerprint of the removed set.
"""

from __future__ import annotations

import re
import unicodedata

# Invisible code points that can carry a hidden message (emoji smuggling in variation
# selectors) and that the "C" categories do not remove.
INVISIBLE = re.compile("[\u115f\u1160\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0\U000e0000-\U000e0fff]")


def strip_invisible(text: str) -> str:
    """Drop what a reader cannot see: format and bidi controls, tags, selectors, fillers, private
    use and unassigned code points, control characters except newline and tab."""
    text = INVISIBLE.sub("", text)
    return "".join(
        char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
