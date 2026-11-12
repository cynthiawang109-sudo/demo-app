# demo-app

A minimal FastAPI implementation of the TaskFlow `api-service`, built to the
contracts in `taskflow-api-docs` (health check, error shape, cursor
pagination). It is the code the rest of the workspace describes, reviews, and
answers questions about.

Part of [ai-workspace-kit](https://github.com/cynthiawang109-sudo/ai-workspace-kit).
Fictional demo — in-memory storage, no real data.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

- `GET /health` → `{"status": "ok"}`
- `POST /v1/tasks` with `{"title": "..."}` → creates a task
- `GET /v1/tasks?cursor=0&limit=50` → cursor-paginated list
- Interactive docs at `/docs`
