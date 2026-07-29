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
