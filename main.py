"""Demo TaskFlow api-service (fictional).

Implements the slice of the TaskFlow API contract that the workspace demos
exercise: the /health contract and a minimal in-memory tasks resource,
following taskflow-api-docs/api-conventions.md.
"""
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="TaskFlow demo api-service", version="1.0.0")

_tasks: dict[int, dict] = {}
_next_id = 1


class TaskIn(BaseModel):
    title: str
    status: str = "todo"


@app.get("/health")
def health():
    # Contract (taskflow-api-docs, "Health checks"): unauthenticated,
    # 200 + {"status": "ok"} when the service can serve traffic.
    return {"status": "ok"}


@app.get("/v1/tasks")
def list_tasks(cursor: int = Query(0), limit: int = Query(50, le=200)):
    ids = sorted(t for t in _tasks if t > cursor)
    page = ids[:limit]
    next_cursor = page[-1] if len(ids) > limit else None
    return {"items": [_tasks[i] for i in page], "next_cursor": next_cursor}


@app.post("/v1/tasks", status_code=201)
def create_task(task: TaskIn):
    global _next_id
    if task.status not in ("todo", "doing", "done"):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "invalid_status",
                              "message": "status must be todo, doing or done"}},
        )
    item = {"id": _next_id, "title": task.title, "status": task.status}
    _tasks[_next_id] = item
    _next_id += 1
    return item


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: int):
    item = _tasks.get(task_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "task_not_found",
                              "message": f"task {task_id} does not exist"}},
        )
    return item
