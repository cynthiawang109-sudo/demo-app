# demo-app

[![tests](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/tests.yml/badge.svg)](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/tests.yml)

A minimal FastAPI implementation of the TaskFlow `api-service`, built to the
contracts in `taskflow-api-docs` (health check, error shape, cursor
pagination). It is the code the rest of the workspace describes, reviews, and
answers questions about.

Part of [ai-workspace-kit](https://github.com/cynthiawang109-sudo/ai-workspace-kit).
Fictional demo — tasks persist in a local SQLite file, no real data.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Tasks are stored in `tasks.db` next to `main.py`; set `TASKFLOW_DB_PATH`
to use a different file. `/health` pings the store and reports
`503 {"status": "degraded", "checks": {"task_store": "down"}}` when the
database cannot be reached.

**Before starting the service, run the migration** (idempotent):

```powershell
.venv\Scripts\python scripts\migrate.py
```

Migrations are a pre-start step (see `taskflow-docs/deployment.md`); the
store never migrates lazily, and `/health` reports degraded against an
unmigrated database — that is how a forgotten migration shows up.

- `GET /health` → `{"status": "ok"}`
- `POST /v1/tasks` with `{"title": "..."}` → creates a task
- `GET /v1/tasks?cursor=0&limit=50` → cursor-paginated list
- `GET /v1/tasks?status=doing&cursor=0&limit=50` → one board column,
  paginated on its own (TF-102; served by the `(status, id)` index)

`scripts/bench_board.py` seeds a throwaway database and measures the
board workload (paging all tasks vs. paging one column) on any checkout,
for before/after comparisons.
