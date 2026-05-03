"""
Launch the job search web UI without running the full pipeline.

Usage:
    python scripts/view_db.py
    python scripts/view_db.py --port 8080
    python scripts/view_db.py --no-browser
    uv run python scripts/view_db.py
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

# UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure CWD is the project root so relative paths (config/, data/) resolve correctly.
_PROJECT_ROOT = Path(__file__).parent.parent  # scripts/view_db.py → project root
if Path.cwd() != _PROJECT_ROOT:
    os.chdir(_PROJECT_ROOT)

# Add src/ to sys.path so job_search is importable without editable install.
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse the job search database.")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config.yaml (default: config/config.yaml)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port to listen on (default: 5000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a browser window automatically")
    args = parser.parse_args()

    from job_search.core.config import load_config
    from job_search.core.database import DatabaseManager
    from job_search.web.app import init_app

    cfg = load_config(args.config)
    db = DatabaseManager(cfg.database.path)
    app = init_app(db, config=cfg)

    url = f"http://{args.host}:{args.port}/"
    print(f"Web UI → {url}  (Ctrl+C to stop)")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if "address already in use" in str(e).lower() or e.errno in (98, 10048):
            print(f"\nError: port {args.port} is already in use.")
            print(f"Try a different port:  python scripts/view_db.py --port {args.port + 1}")
            print("Or stop the other process first (e.g. the running pipeline).")
        else:
            raise
    finally:
        db.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
