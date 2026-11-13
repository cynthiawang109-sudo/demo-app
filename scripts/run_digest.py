"""Run the weekly digest job once (worker demo entry point).

Usage (from the demo-app root, after scripts\\migrate.py):

    .venv\\Scripts\\python scripts\run_digest.py --user demo-user --week 2026-W46

Deliveries go through the demo OutboxSender, which dedupes on the
idempotency key, so re-running this command for the same user and
week never sends twice. The outcome (sent / already_sent /
dead_lettered) and the attempt count are printed as JSON.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest import OutboxSender, run_digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True,
                        help="recipient user ID (never an email address)")
    parser.add_argument("--week", required=True,
                        help="ISO week key, e.g. 2026-W46")
    parser.add_argument("--db", default=None,
                        help="SQLite path (default: TASKFLOW_DB_PATH"
                             " or ./taskflow.db)")
    parser.add_argument("--outbox", default=None,
                        help="outbox file (default: digest-outbox.jsonl"
                             " next to the database)")
    args = parser.parse_args()

    # Attempt logs (TF-103: log each attempt with the user ID) go to
    # stderr; stdout stays reserved for the outcome JSON.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr)

    db_path = args.db or os.environ.get("TASKFLOW_DB_PATH", "taskflow.db")
    outbox = args.outbox or str(
        Path(db_path).resolve().parent / "digest-outbox.jsonl")
    result = run_digest(db_path, args.user, args.week,
                        OutboxSender(outbox))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
