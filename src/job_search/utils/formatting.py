from __future__ import annotations

import re


def clean_cover_letter_text(text: str | None) -> str:
    """
    Clean cover letter text by normalizing line endings and removing extra blank lines.
    
    Replaces multiple consecutive newlines (2 or more) with a single newline (\\n)
    so that pasting into MS Word uses standard paragraph breaks without empty line gaps.
    """
    if not text:
        return ""
    # Normalize Windows/Mac line endings
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Replace multiple consecutive newlines (possibly with whitespace) with single \n
    cleaned = re.sub(r"\n\s*\n+", "\n", normalized)
    return cleaned.strip()


def normalize_message_text(text: str | None) -> str:
    """Tidy a short message while KEEPING its paragraph breaks.

    Deliberately not clean_cover_letter_text: that one collapses blank lines
    because Word wants single newlines between paragraphs. An InMail is read
    on a phone, where the blank line is the only thing separating one thought
    from the next. So runs of three or more newlines collapse to exactly one
    blank line, and a single blank line survives untouched.
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing spaces a model likes to leave at line ends.
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    collapsed = re.sub(r"\n{3,}", "\n\n", normalized)
    return collapsed.strip()
