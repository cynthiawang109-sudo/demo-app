"""Demo TaskFlow api-service (fictional).

Implements the slice of the TaskFlow API contract that the workspace demos
exercise: the /health contract and a minimal tasks resource backed by a
local SQLite file, following taskflow-api-docs/api-conventions.md.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class AccessLogMiddleware:
    """TF-109: request IDs and one JSON access record, without buffering."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # taskflow-docs/observability.md ("Logs"): propagate the edge ID;
        # TF-109 mints one here when the demo has no upstream edge.
        request_id = Headers(scope=scope).get("x-request-id", str(uuid4()))
        started = perf_counter()
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # observability.md: PII in logs is severity 1. Explicit fields
            # only: no bodies, query string, or other headers. The demo
            # has no authenticated user context, so user_id is omitted.
            print(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "service": "demo-app",
                "level": "error" if 500 <= status < 600 else "info",
                "request_id": request_id,
                "method": scope["method"],
                "path": getattr(scope.get("route"), "path", "<unmatched>"),
                "status": status,
                "duration_ms": (perf_counter() - started) * 1000,
            }), flush=True)

api = FastAPI(title="TaskFlow demo api-service", version="1.0.0")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tasks ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "title TEXT NOT NULL, status TEXT NOT NULL)"
)

# Supports the board's per-column reads (TF-102): status-filtered cursor
# pagination walks this index instead of filtering the primary-key scan
# row by row. Expand-only, like every schema change here.
_INDEX = "CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks (status, id)"

_COMMENTS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS comments ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "task_id INTEGER NOT NULL REFERENCES tasks(id), "
    "body TEXT NOT NULL, supersedes_id INTEGER REFERENCES comments(id))"
)
_COMMENTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_comments_task_id_id ON comments (task_id, id)"
)

STATUSES = ("todo", "doing", "done")


class CommentNotFound(Exception):
    """The revision does not exist within the requested task."""


class CommentConflict(Exception):
    """The revision has already been superseded."""


def migrate(db_path: str) -> None:
    """Apply the schema (tables + indexes) to the database at db_path.

    Idempotent, and deliberately NOT done lazily by the store:
    taskflow-docs/deployment.md requires migrations to run *before*
    the service starts serving traffic, so `scripts/migrate.py` is a
    pre-start step. A store pointed at an unmigrated database fails
    its operations - /health reports degraded - instead of silently
    migrating mid-request.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.execute(_INDEX)
        conn.execute(_COMMENTS_SCHEMA)
        conn.execute(_COMMENTS_INDEX)
        from digest import DIGEST_SCHEMA  # the worker's ledger table

        conn.execute(DIGEST_SCHEMA)
        conn.commit()
    finally:
        conn.close()


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
        """Raise if the store cannot serve reads.

        Probes the tasks and comments tables, not just the connection:
        a "SELECT 1" would succeed even when required tables are missing
        or unreadable, reporting healthy while requests fail.
        """
        with self._connect() as conn:
            conn.execute("SELECT id FROM tasks LIMIT 1")
            conn.execute("SELECT id FROM comments LIMIT 1")

    def list_page(
        self, cursor: int, limit: int, status: str | None = None
    ) -> tuple[list[dict], int | None]:
        # Fetch one extra row to know whether another page exists. With a
        # status filter (a board column), the (status, id) index serves
        # the same cursor walk inside one column.
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT id, title, status FROM tasks "
                    "WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, limit + 1),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title, status FROM tasks "
                    "WHERE status = ? AND id > ? ORDER BY id LIMIT ?",
                    (status, cursor, limit + 1),
                ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = items[-1]["id"] if len(rows) > limit else None
        return items, next_cursor

    def create(self, title: str, status: str) -> dict:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, status) VALUES (?, ?)", (title, status)
            )
            task_id = cur.lastrowid
        return {"id": task_id, "title": title, "status": status}

    def get(self, task_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def create_comment(
        self, task_id: int, body: str, supersedes_id: int | None = None
    ) -> dict:
        # taskflow-api-docs/data-model.md ("comments"): revisions append
        # rows; the previous body is never updated. TF-106 requires a
        # chain head: serialize the check and insert to prevent forks.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if supersedes_id is not None:
                previous = conn.execute(
                    "SELECT id FROM comments WHERE task_id = ? AND id = ?",
                    (task_id, supersedes_id),
                ).fetchone()
                if previous is None:
                    raise CommentNotFound
                successor = conn.execute(
                    "SELECT id FROM comments WHERE supersedes_id = ? LIMIT 1",
                    (supersedes_id,),
                ).fetchone()
                if successor is not None:
                    raise CommentConflict
            cur = conn.execute(
                "INSERT INTO comments (task_id, body, supersedes_id) VALUES (?, ?, ?)",
                (task_id, body, supersedes_id),
            )
            comment_id = cur.lastrowid
        return {
            "id": comment_id, "task_id": task_id,
            "body": body, "supersedes_id": supersedes_id,
        }

    def list_comments_page(
        self, task_id: int, cursor: int, limit: int
    ) -> tuple[list[dict], int | None]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_id, body, supersedes_id FROM comments "
                "WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                (task_id, cursor, limit + 1),
            ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = items[-1]["id"] if len(rows) > limit else None
        return items, next_cursor

    def get_comment(self, task_id: int, comment_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, task_id, body, supersedes_id FROM comments "
                "WHERE task_id = ? AND id = ?",
                (task_id, comment_id),
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


@api.exception_handler(sqlite3.Error)
def store_error_handler(request: Request, exc: sqlite3.Error) -> JSONResponse:
    # A down store must still answer in the contract error envelope,
    # never FastAPI's default 500 body. /health is exempt by design: it
    # catches its own ping failures and reports the degraded shape.
    return _error(503, "store_unavailable", "task store is unavailable")


@api.exception_handler(RequestValidationError)
def validation_error_handler(request: Request,
                             exc: RequestValidationError) -> JSONResponse:
    # Framework validation failures (a non-integer limit, a missing
    # title, malformed JSON) must also answer in the contract
    # envelope with 422 (TF-105) - never FastAPI's default "detail"
    # shape. The message names the field and the problem; it never
    # echoes the submitted value.
    errors = exc.errors()
    if errors:
        first = errors[0]
        field = ".".join(
            str(part) for part in first["loc"]
            if part not in ("query", "body", "path")
        )
        message = f"{field}: {first['msg']}" if field else first["msg"]
    else:
        message = "request validation failed"
    return _error(422, "validation_error", message)


class TaskIn(BaseModel):
    title: str
    status: str = "todo"


class CommentIn(BaseModel):
    body: str
    supersedes_id: int | None = None


@api.get("/health")
def health():
    # Contract (taskflow-api-docs, "Health checks"): unauthenticated;
    # 200 + {"status": "ok"} when the service can serve traffic, 503 +
    # {"status": "degraded", "checks": {"task_store": "down"}} when a hard
    # dependency is unreachable. No version, host, or dependency details
    # beyond the check names.
    try:
        store.ping()
    except Exception:  # noqa: BLE001 - any ping failure means degraded
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "checks": {"task_store": "down"}},
        )
    return {"status": "ok"}


def _clamp_pagination(cursor: int, limit: int) -> tuple[int, int]:
    return max(cursor, 0), min(max(limit, 1), 200)


@api.get("/v1/tasks")
def list_tasks(
    cursor: int = Query(0),
    limit: int = Query(50),
    status: str | None = Query(None),
):
    if status is not None and status not in STATUSES:
        return _error(422, "invalid_status", "status must be todo, doing or done")
    # Clamp instead of erroring: pagination inputs come from clients
    # that page in a loop, and an out-of-range value must never wedge
    # one. (Recorded pre-existing issue: only the limit's upper bound
    # was enforced - limit=0 looped a paging client forever and a
    # negative cursor silently restarted the walk.)
    cursor, limit = _clamp_pagination(cursor, limit)
    items, next_cursor = store.list_page(cursor, limit, status)
    return {"items": items, "next_cursor": next_cursor}


@api.post("/v1/tasks", status_code=201)
def create_task(task: TaskIn):
    if task.status not in STATUSES:
        return _error(422, "invalid_status", "status must be todo, doing or done")
    return store.create(task.title, task.status)


@api.get("/v1/tasks/{task_id}")
def get_task(task_id: int):
    item = store.get(task_id)
    if item is None:
        return _error(404, "task_not_found", f"task {task_id} does not exist")
    return item


@api.post("/v1/tasks/{task_id}/comments", status_code=201)
def create_comment(task_id: int, comment: CommentIn):
    if store.get(task_id) is None:
        return _error(404, "task_not_found", f"task {task_id} does not exist")
    # TF-106; taskflow-api-docs/api-conventions.md ("Errors"):
    # reject blank text in the contract envelope without echoing it.
    if not comment.body.strip():
        return _error(422, "validation_error", "comment body must not be blank")
    try:
        return store.create_comment(task_id, comment.body, comment.supersedes_id)
    except CommentNotFound:
        return _error(404, "comment_not_found", "comment does not exist")
    except CommentConflict:
        return _error(409, "conflict", "comment revision is already superseded")


@api.get("/v1/tasks/{task_id}/comments")
def list_comments(
    task_id: int, cursor: int = Query(0), limit: int = Query(50)
):
    if store.get(task_id) is None:
        return _error(404, "task_not_found", f"task {task_id} does not exist")
    # taskflow-api-docs/api-conventions.md ("Pagination"): same cursor
    # walk and bounds as tasks; include the full append-only revision log.
    cursor = max(cursor, 0)
    limit = min(max(limit, 1), 200)
    items, next_cursor = store.list_comments_page(task_id, cursor, limit)
    return {"items": items, "next_cursor": next_cursor}


@api.get("/v1/tasks/{task_id}/comments/{comment_id}")
def get_comment(task_id: int, comment_id: int):
    if store.get(task_id) is None:
        return _error(404, "task_not_found", f"task {task_id} does not exist")
    item = store.get_comment(task_id, comment_id)
    if item is None:
        return _error(404, "comment_not_found", "comment does not exist")
    return item


# Wrap the complete FastAPI stack, including ServerErrorMiddleware, so even
# unhandled 500 responses receive the same request ID as the access record.
# Keep `api` available for framework operations such as api.openapi().
app = AccessLogMiddleware(api)
