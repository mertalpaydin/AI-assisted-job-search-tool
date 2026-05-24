"""
Analyze job title keywords to tune the search title filter.

Usage:
    python utils/analyze_title_keywords.py
    python utils/analyze_title_keywords.py --db data/jobs.db --top 80

The script reads the current allowlist from config/config.yaml and simulates
how it would perform against the existing database. Adjust `title_filter.require_any`
in config.yaml and re-run to iterate.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_allowlist(config_path: str = "config/config.yaml") -> list[str]:
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return data.get("search", {}).get("title_filter", {}).get("require_any", [])


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def passes_filter(title: str, keywords: list[str]) -> bool:
    """Mirror the exact regex used in SearchWorker._title_passes_filter."""
    t = title.lower()
    return any(
        re.search(r"(?<![a-z])" + re.escape(kw.lower()), t)
        for kw in keywords
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze title keywords for the search filter.")
    parser.add_argument("--db", default="data/jobs.db", help="Path to jobs SQLite database")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    parser.add_argument("--top", type=int, default=60, help="How many top words to show")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    cur.execute("SELECT title FROM jobs WHERE is_selected = 1 AND title IS NOT NULL")
    sel_titles = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT title FROM jobs WHERE is_selected = 0 AND cv_match_score IS NOT NULL AND title IS NOT NULL"
    )
    rej_titles = [r[0] for r in cur.fetchall()]
    conn.close()

    print(f"Selected jobs: {len(sel_titles)}")
    print(f"Rejected jobs: {len(rej_titles)}")

    # -----------------------------------------------------------------------
    # Word frequency in selected titles
    # -----------------------------------------------------------------------
    STOP = {
        "and", "or", "the", "a", "an", "in", "of", "for", "to", "with", "at",
        "by", "on", "as", "is", "be", "are", "was", "were", "from", "this",
        "that", "it", "its", "we", "you", "mwd", "fmd", "mf", "fmx", "wmx",
        "mfx", "mwx", "wmd", "fw", "mw", "m", "w", "f", "d", "all", "im",
        "de", "am", "nd", "st", "rd", "th", "co", "inc", "ltd", "gmbh", "fte",
    }

    def word_freq(titles: list[str]) -> Counter:
        words = []
        for t in titles:
            words.extend(re.findall(r"[a-zA-Z]+", t.lower()))
        return Counter(w for w in words if w not in STOP and len(w) > 2)

    sel_freq = word_freq(sel_titles)
    rej_freq = word_freq(rej_titles)

    print(f"\n{'='*60}")
    print(f"TOP {args.top} WORDS IN SELECTED JOB TITLES")
    print(f"{'='*60}")
    for word, cnt in sel_freq.most_common(args.top):
        rej_cnt = rej_freq.get(word, 0)
        print(f"  {cnt:5d}  {word:25s}  (in rejected: {rej_cnt})")

    # -----------------------------------------------------------------------
    # Junk signal: high in rejected, low in selected
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("WORDS COMMON IN REJECTED BUT RARE IN SELECTED")
    print(f"{'='*60}")
    junk = {
        w: (r, sel_freq.get(w, 0))
        for w, r in rej_freq.items()
        if r >= 30 and r / max(sel_freq.get(w, 0), 1) >= 10
    }
    for word, (r, s) in sorted(junk.items(), key=lambda x: -x[1][0] / max(x[1][1], 1))[:30]:
        ratio = r / max(s, 1)
        print(f"  {word:25s}  rejected={r:4d}  selected={s:3d}  ratio={ratio:.0f}x")

    # -----------------------------------------------------------------------
    # Simulate current allowlist from config
    # -----------------------------------------------------------------------
    allowlist = load_allowlist(args.config)
    if not allowlist:
        print("\nNo title_filter.require_any configured in config.yaml — skipping simulation.")
        return

    print(f"\n{'='*60}")
    print(f"FILTER SIMULATION  ({len(allowlist)} keywords)")
    print(f"{'='*60}")
    print(f"Keywords: {allowlist}")
    print()

    sel_pass  = [t for t in sel_titles if passes_filter(t, allowlist)]
    sel_block = [t for t in sel_titles if not passes_filter(t, allowlist)]
    rej_block = [t for t in rej_titles if not passes_filter(t, allowlist)]
    rej_pass  = [t for t in rej_titles if passes_filter(t, allowlist)]

    print(f"Selected KEPT:     {len(sel_pass):5d}/{len(sel_titles)} ({100*len(sel_pass)/max(len(sel_titles),1):.1f}%)")
    print(f"Selected BLOCKED:  {len(sel_block):5d}/{len(sel_titles)} ({100*len(sel_block)/max(len(sel_titles),1):.1f}%)")
    print(f"Rejected BLOCKED:  {len(rej_block):5d}/{len(rej_titles)} ({100*len(rej_block)/max(len(rej_titles),1):.1f}%)")
    print(f"Rejected PASSED:   {len(rej_pass):5d}/{len(rej_titles)} ({100*len(rej_pass)/max(len(rej_titles),1):.1f}%)")
    print(f"\nEstimated API calls saved: {len(rej_block)} out of {len(rej_titles)} rejections")

    if sel_block:
        print(f"\n--- Selected jobs that WOULD BE BLOCKED ({len(sel_block)}) ---")
        for t in sorted(set(sel_block)):
            print(f"  {t}")


if __name__ == "__main__":
    main()