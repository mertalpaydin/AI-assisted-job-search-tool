"""Lightweight, deterministic language detection based on stopword distribution.

Distinguishes postings written predominantly in German from postings written
primarily in English with incidental German snippets (such as legal disclaimers,
company location boilerplate, or language requirements).
"""
from __future__ import annotations

import re

# High-frequency German function words (stopwords) that rarely appear in English text.
GERMAN_STOPWORDS: frozenset[str] = frozenset({
    "der", "die", "das", "und", "in", "den", "von", "zu", "mit", "sich", "des",
    "auf", "für", "fuer", "ist", "im", "dem", "nicht", "ein", "eine", "als",
    "auch", "es", "an", "werden", "aus", "er", "hat", "dass", "sie", "nach",
    "wird", "bei", "einer", "um", "am", "sind", "noch", "wie", "einem", "über",
    "ueber", "einen", "so", "ihr", "war", "wir", "was", "ihrer", "zum", "zur",
    "diese", "dieser", "dieses", "diesen", "diesem", "durch", "oder", "aber",
    "vor", "man", "da", "du", "dich", "dir", "dein", "deine", "uns", "unser",
    "unsere", "unserem", "unseren", "unseres", "ihre", "ihren", "ihrem", "ihres",
    "haben", "hatte", "hatten", "können", "koennen", "kann", "müssen", "muessen",
    "muss", "soll", "sollte", "welche", "welcher", "welches", "wir", "ihnen",
    "wurde", "wurden", "zwischen", "unter", "ohne", "gegen", "während", "waehrend",
})

# High-frequency English function words that rarely appear in German text.
ENGLISH_STOPWORDS: frozenset[str] = frozenset({
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "it", "for",
    "not", "on", "with", "he", "as", "you", "do", "at", "this", "but", "his",
    "by", "from", "they", "we", "say", "her", "she", "or", "an", "will", "my",
    "one", "all", "would", "there", "their", "what", "so", "up", "out", "if",
    "about", "who", "which", "can", "into", "your", "some", "could", "them",
    "other", "than", "then", "now", "only", "its", "over", "also", "after",
    "use", "how", "our", "work", "even", "because", "any", "these", "most", "us",
    "been", "has", "were", "are", "is", "should", "would", "each", "more",
})

_WORD_RE = re.compile(r"\b[a-zA-ZäöüßÄÖÜ]+\b")


def get_stopword_counts(text: str | None) -> tuple[int, int]:
    """Return (german_count, english_count) for stopwords found in text."""
    if not text:
        return 0, 0
    words = _WORD_RE.findall(text.lower())
    de_count = sum(1 for w in words if w in GERMAN_STOPWORDS)
    en_count = sum(1 for w in words if w in ENGLISH_STOPWORDS)
    return de_count, en_count


def detect_german_ratio(text: str | None) -> float:
    """Calculate the ratio of German stopwords to total (German + English) stopwords.

    Returns a float in [0.0, 1.0]. A pure English text with minor German snippets
    typically scores < 0.15. A posting written in German scores > 0.85.
    Returns 0.0 if neither German nor English stopwords are detected.
    """
    de_count, en_count = get_stopword_counts(text)
    total = de_count + en_count
    if total == 0:
        return 0.0
    return round(de_count / total, 3)


def is_predominantly_german(text: str | None, threshold: float = 0.50) -> bool:
    """Return True if the text is predominantly written in German.

    Uses a default threshold of 0.50 (i.e. German stopwords outnumber English stopwords).
    """
    return detect_german_ratio(text) >= threshold


def detect_language(text: str | None, threshold: float = 0.50) -> tuple[str, float]:
    """Return (language_code, german_ratio) where language_code is 'de', 'en', or 'unknown'."""
    if not text or not text.strip():
        return "unknown", 0.0
    de_count, en_count = get_stopword_counts(text)
    total = de_count + en_count
    if total < 5:
        # Insufficient stopword evidence to classify confidently
        return "unknown", 0.0
    ratio = round(de_count / total, 3)
    lang = "de" if ratio >= threshold else "en"
    return lang, ratio
