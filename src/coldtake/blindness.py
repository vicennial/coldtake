"""Blindness checks for packets sent to blind modes."""

from __future__ import annotations

import re
from collections.abc import Iterable

CONTAMINATION = re.compile(
    r"\b(?:my|our|the\s+lead(?:['’]s)?|the\s+primary|the\s+other\s+agent['’]?s?)\s+"
    r"(?:answer|recommendation|conclusion|ranking|pick|view)\b|"
    r"\bI\s+(?:think|believe|recommend|suggest|propose)\s+(?:we|you)\s+should\b|"
    r"\bI(?:['’]m|\s+am)\s+(?:leaning|inclined)\s+(?:to|toward|towards)\b|"
    r"\bthe\s+(?:consensus|majority)\b|"
    r"\b(?:consensus|majority)\s+(?:is|says|recommends|favou?rs)\b",
    re.IGNORECASE,
)

OVERLAP_WORDS = 12
_WORD = re.compile(r"\w+", re.UNICODE)


def has_contamination(texts: Iterable[str]) -> bool:
    return any(CONTAMINATION.search(text) for text in texts)


def _ngrams(text: str, size: int) -> set[tuple[str, ...]]:
    words = [word.casefold() for word in _WORD.findall(text)]
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def sealed_overlap_warnings(sealed_answer: str, visible_texts: Iterable[str]) -> list[str]:
    """Advisory only: long verbatim spans of the sealed answer inside the visible packet.

    A packet that quotes the sealed answer anchors the advisor and defeats the
    blind comparison. Only counts are reported, never the quoted span.
    """
    sealed = _ngrams(sealed_answer, OVERLAP_WORDS)
    if not sealed:
        return []
    hits = sum(len(sealed & _ngrams(text, OVERLAP_WORDS)) for text in visible_texts)
    if not hits:
        return []
    return [
        f"sealed_answer_overlap: {hits} verbatim span(s) of {OVERLAP_WORDS}+ words from the "
        "sealed answer also appear in the question or context; the advisor may be anchored"
    ]
