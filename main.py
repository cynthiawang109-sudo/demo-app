"""Demo TaskFlow api-service (fictional).

Implements the slice of the TaskFlow API contract that the workspace demos
exercise: the /health contract and a minimal in-memory tasks resource,
following taskflow-api-docs/api-conventions.md.
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="TaskFlow demo api-service", version="1.0.0")


class TaskStore:
    """In-memory task storage.

    The store is the service's data dependency: /health pings it, and a
    failed ping means the service cannot serve traffic. A persistent
    store would round-trip its backend in ping() (e.g. SELECT 1); the
    in-memory implementation can only fail if the store object itself is
    broken, which is exactly what the check exists to catch.
    """

    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}
        self.next_id = 1

    def ping(self) -> None:
        """Raise if the store cannot serve reads."""
        _ = len(self.tasks)


store = TaskStore()


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
    ids = sorted(t for t in store.tasks if t > cursor)
    page = ids[:limit]
    next_cursor = page[-1] if len(ids) > limit else None
    return {"items": [store.tasks[i] for i in page], "next_cursor": next_cursor}


@app.post("/v1/tasks", status_code=201)
def create_task(task: TaskIn):
    if task.status not in ("todo", "doing", "done"):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "invalid_status",
                              "message": "status must be todo, doing or done"}},
        )
    item = {"id": store.next_id, "title": task.title, "status": task.status}
    store.tasks[store.next_id] = item
    store.next_id += 1
    return item


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: int):
    item = store.tasks.get(task_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "task_not_found",
                              "message": f"task {task_id} does not exist"}},
        )
    return item
