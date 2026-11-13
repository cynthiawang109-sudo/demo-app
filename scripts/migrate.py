"""Pre-start database migration for the demo api-service.

Applies the schema (tasks table + indexes) to the SQLite database.
Idempotent: safe to run on every deploy, including against an existing
populated database. Per taskflow-docs/deployment.md, migrations run
*before* the service starts - the store never migrates lazily, and
/health reports degraded against an unmigrated database.

Usage:
    python scripts/migrate.py [db-path]

The database path defaults to TASKFLOW_DB_PATH, or tasks.db next to
main.py - the same resolution the service itself uses.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import migrate  # noqa: E402


def default_db_path() -> str:
    return os.environ.get(
        "TASKFLOW_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "tasks.db"),
    )


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else default_db_path()
    migrate(path)
    print(f"migrated: {path}")
