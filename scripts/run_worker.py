"""Drain currently due jobs once, after running scripts/migrate.py."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import default_handlers, process_due


def main() -> None:
    db_path = os.environ.get(
        "TASKFLOW_DB_PATH", str(Path(__file__).resolve().parent.parent / "tasks.db")
    )
    processed = process_due(db_path, default_handlers(db_path), time.time())
    print(json.dumps({"processed": processed}))


if __name__ == "__main__":
    main()
