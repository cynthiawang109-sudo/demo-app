"""Drain currently due jobs once, after running scripts/migrate.py."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import default_handlers, process_due, recover_interrupted


def main() -> None:
    db_path = os.environ.get(
        "TASKFLOW_DB_PATH", str(Path(__file__).resolve().parent.parent / "tasks.db")
    )
    now = time.time()
    # This one-shot script is the only worker process, so running rows are
    # interrupted by construction. Recover only when no other worker is active.
    recover_interrupted(db_path, now)
    processed = process_due(db_path, default_handlers(db_path), now)
    print(json.dumps({"processed": processed}))


if __name__ == "__main__":
    main()
