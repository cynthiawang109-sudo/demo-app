"""Demo TaskFlow api-service (fictional).

Implements the slice of the TaskFlow API contract that the workspace demos
exercise: the /health contract and a minimal tasks resource backed by a
local SQLite file, following taskflow-api-docs/api-conventions.md.
"""
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="TaskFlow demo api-service", version="1.0.0")


class TaskStore:
    """SQLite-backed task storage.

    The store is the service's data dependency: /health pings it, and a
    failed ping means the service cannot serve traffic. Connections are
    short-lived (one per operation), which keeps the store safe to share
    across FastAPI's threadpool. The database file path comes from the
    TASKFLOW_DB_PATH environment variable (default: tasks.db next to
    this module).
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ping(self) -> None:
        """Raise if the store cannot serve reads."""
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def list_page(self, cursor: int, limit: int) -> tuple[list[dict], int | None]:
        # Fetch one extra row to know whether another page exists.
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, status TEXT NOT NULL)"
            )
            rows = conn.execute(
                "SELECT id, title, status FROM tasks "
                "WHERE id > ? ORDER BY id LIMIT ?",
                (cursor, limit + 1),
            ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = items[-1]["id"] if len(rows) > limit else None
        return items, next_cursor

    def create(self, title: str, status: str) -> dict:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, status TEXT NOT NULL)"
            )
            cur = conn.execute(
                "INSERT INTO tasks (title, status) VALUES (?, ?)", (title, status)
            )
            task_id = cur.lastrowid
        return {"id": task_id, "title": title, "status": status}

    def get(self, task_id: int) -> dict | None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, status TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT id, title, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None


store = TaskStore(
    os.environ.get("TASKFLOW_DB_PATH", str(Path(__file__).with_name("tasks.db")))
)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    # Error envelope per taskflow-api-docs ("Errors"): a top-level "error"
    # object with a stable code. Raising HTTPException(detail=...) would
    # wrap it in "detail", which is off-contract.
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


class TaskIn(BaseModel):
    title: str
    status: str = "todo"


@app.get("/health")
def health():
    # Contract (taskflow-api-docs, "Health checks"): unauthenticated;
    # 200 + {"status": "ok"} when the service can serve traffic, 503 +
    # {"status": "degraded", "checks": {"task_store": "down"}} when a hard
    # dependency is unreachable. No version, host, or dependency details
    # beyond the check names.
    try:
        store.ping()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "checks": {"task_store": "down"}},
        )
    return {"status": "ok"}


@app.get("/v1/tasks")
def list_tasks(cursor: int = Query(0), limit: int = Query(50, le=200)):
    items, next_cursor = store.list_page(cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@app.post("/v1/tasks", status_code=201)
def create_task(task: TaskIn):
    if task.status not in ("todo", "doing", "done"):
        return _error(422, "invalid_status", "status must be todo, doing or done")
    return store.create(task.title, task.status)


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: int):
    item = store.get(task_id)
    if item is None:
        return _error(404, "task_not_found", f"task {task_id} does not exist")
    return item
