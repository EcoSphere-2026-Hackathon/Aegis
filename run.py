#!/usr/bin/env python3
"""
AEGIS entrypoint.

    python run.py                 # serve the console and the API
    python run.py --check         # validate configuration and exit
    python run.py --demo          # replay the golden demo in the terminal

Kept deliberately small: it loads configuration, reports what it is about to
do, and hands off. Anything that needs to happen before the first transcript
arrives -- credential checks, provider selection, schema migration -- happens
here rather than mid-incident.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.common.config import iter_config_summary, load_config  # noqa: E402
from backend.common.errors import ConfigError  # noqa: E402
from backend.common.logging import (  # noqa: E402
    configure_logging,
    get_logger,
    log_startup_banner,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS — voice-native incident commander")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--demo", action="store_true", help="replay the golden demo in the terminal")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the stored incident before starting (the demo must be runnable twice)",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level, log_file=config.log_file)
    log = get_logger("main")

    if args.check:
        print("AEGIS configuration\n")
        for key, value in iter_config_summary(config):
            print(f"  {key:32} {value}")
        warnings = config.warnings()
        if warnings:
            print("\nWarnings:")
            for warning in warnings:
                print(f"  ! {warning}")
        else:
            print("\nNo configuration warnings.")
        return 0

    if args.demo:
        from scripts.run_golden_demo import run_golden_demo

        return 0 if run_golden_demo() else 1

    if args.reset:
        # Before anything else opens the database. The default store is a
        # file, so a second run of the rehearsed demo would otherwise start
        # from the first run's pending actions and stale theories.
        from backend.state_store.store import IncidentStateStore

        with IncidentStateStore(config.database_path, incident_id=config.incident_id) as store:
            store.reset_incident()
        print(f"  incident {config.incident_id} cleared ({config.database_path})")

    import uvicorn

    from backend.api.app import create_app

    host = args.host or config.api.host
    port = args.port or config.api.port

    log_startup_banner(log, dict(iter_config_summary(config)), config.warnings())
    # ASCII only. A Windows console defaults to cp1252, and printing a "→"
    # here raised UnicodeEncodeError *before* uvicorn.run -- the process
    # exited without ever binding the port, which reads as "the server is
    # broken" rather than "the banner is".
    print(f"\n  AEGIS landing  -> http://{host}:{port}/")
    print(f"  AEGIS console  -> http://{host}:{port}/command\n")

    uvicorn.run(create_app(config), host=host, port=port, log_level=config.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
