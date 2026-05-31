"""
Standalone LinkedIn login test script.

Usage:
    uv run python scripts/test_login.py
    uv run python scripts/test_login.py --attempts 5 --browser edge

The script attempts login up to N times, pauses between failures, and reports
exactly which selectors worked (or didn't).  On any failure it writes a
screenshot + partial page-source to ./debug/ so you can see what LinkedIn
actually served.

Exit codes:
    0 — login succeeded
    1 — all attempts failed
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running from the repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from loguru import logger
from job_search.core.config import load_config, load_secrets
from job_search.scraping.auth import create_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Test LinkedIn login")
    parser.add_argument("--config",   default="config/config.yaml")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--delay",    type=int, default=15,
                        help="Seconds between retry attempts")
    parser.add_argument("--browser",  default=None,
                        help="Browser override (chrome / edge / firefox)")
    args = parser.parse_args()

    cfg     = load_config(args.config)
    secrets = load_secrets()

    if not secrets.linkedin_username or not secrets.linkedin_password:
        logger.error(
            "LinkedIn credentials not set. "
            "Add LINKEDIN_USERNAME and LINKEDIN_PASSWORD to config/.env"
        )
        return 1

    browser = args.browser or "edge"
    logger.info("Testing LinkedIn login for {} with {} browser…",
                secrets.linkedin_username, browser)

    for attempt in range(1, args.attempts + 1):
        logger.info("─── Attempt {}/{} ───", attempt, args.attempts)
        session = create_session(
            email=secrets.linkedin_username,
            password=secrets.linkedin_password,
            browser=browser,
            attempt=attempt,
        )
        if session is not None:
            logger.success(
                "Login SUCCEEDED on attempt {}. Session cookies: {}",
                attempt,
                list(session.cookies.keys()),
            )
            return 0

        if attempt < args.attempts:
            logger.warning("Attempt {} failed — waiting {}s before retry…",
                           attempt, args.delay)
            time.sleep(args.delay)

    logger.error(
        "All {} login attempts failed.\n"
        "  • Check debug/ for screenshots and page-source snapshots.\n"
        "  • Verify LINKEDIN_USERNAME / LINKEDIN_PASSWORD in config/.env\n"
        "  • Try opening https://www.linkedin.com/login in a browser and "
        "inspecting the username/password field attributes.",
        args.attempts,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
