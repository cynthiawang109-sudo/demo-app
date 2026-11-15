# demo-app

[![tests](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/ci.yml/badge.svg)](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/ci.yml)
[![CodeQL](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/codeql.yml/badge.svg)](https://github.com/cynthiawang109-sudo/demo-app/actions/workflows/codeql.yml)

A minimal FastAPI implementation of the TaskFlow `api-service`, built to the
contracts in `taskflow-api-docs` (health check, error shape, cursor
pagination). It is the code the rest of the workspace describes, reviews, and
answers questions about.

Part of [ai-workspace-kit](https://github.com/cynthiawang109-sudo/ai-workspace-kit).
Fictional demo — tasks persist in a local SQLite file, no real data.

## Live demo

The app is deployed on Render's free tier: https://taskflow-demo-app.onrender.com
(health check: https://taskflow-demo-app.onrender.com/health). The free tier sleeps when idle, so the first request may take up to
a minute. The SQLite database is ephemeral and resets on redeploy; demo
data is disposable and fictional.

The bare domain root returns a small JSON index linking to `/health`, `/docs`, `/v1/tasks`, and `/openapi.json`.

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

## Weekly digest (worker slice)

`digest.py` is the demo slice of the worker's weekly digest job
(TF-103). Run it after `scripts/migrate.py`:

    .venv\Scripts\python scripts\run_digest.py --user demo-user --week 2026-W46

Sends are idempotent per (user, week) — the ledger table
`digest_sends` (that pair is its primary key) plus an idempotency key
the sender dedupes on — every attempt is recorded and logged with the
user ID (never an email address), and after 5 attempts the row is
dead-lettered and left alone until a human resets it. (Diagnosis and
reset steps: `taskflow-docs/incident-runbook.md`.) The demo sender
appends to `digest-outbox.jsonl` next to the database.
## Worker

`worker.py` queues scalar JSON payloads containing IDs (no `email` key) in
the `jobs` table, created by the same pre-start migration. Enqueue with
`worker.enqueue(db_path_or_store, "weekly_digest", {"user_id": "demo-user",
"week": "2026-W46"})`, then drain all currently due jobs once:

    .venv\Scripts\python scripts\run_worker.py

The worker uses `TASKFLOW_DB_PATH` (default: `tasks.db` next to `main.py`).
Failed jobs retry after `min(2 ** attempts, 300)` seconds; the fifth failed
attempt dead-letters the job and later drains leave it alone. Lifecycle
JSON logs carry the job's request ID and, when the payload contains an integer
ID or a validated string ID matching `[A-Za-z0-9_-]{1,64}`, that value as
`user_id`. No other payload fields are logged. The built-in
`weekly_digest` handler reuses the digest send path, ledger, and outbox above,
including its existing internal retries and terminal outcomes.

At startup the script recovers every `running` job as interrupted: it increments
attempts, records `InterruptedError`, and queues it with the normal backoff,
or dead-letters it when attempts reach 5. This one-shot demo assumes the script
is the only worker process, so any `running` row at startup is interrupted.
Recovery must only run when no other worker process is active. For
`weekly_digest`, the digest ledger makes replay safe: an already-sent user/week
is skipped.

The production worker service exposes `GET /health` per the API conventions.
As an explicit exception, the demo's one-shot drain script is not a service
and exposes no `GET /health`; its exit code and summary line are its health
signal.

The HTTP API and `/health` check are unchanged. The worker is a separate
process; in the demo it shares the SQLite file with the API.

## Container

CI failures and published releases post to the workspace Slack channel via the
shared `cynthiawang109-sudo/ai-workspace-kit/.github/workflows/slack-notify.yml@052914a8eab5dfcc5a05b2087f7c7942f206065c`
reusable workflow, pinned to a reviewed commit SHA, and this repo's
`SLACK_WEBHOOK_URL` Actions secret. Bumping the pin is a reviewed change.

A `Dockerfile` packages the service (deployment.md: merge to `main`
triggers tests, lint, and a container build in CI). The entrypoint
runs the migration before starting the server, per the same
contract; the database lives on the `/data` volume:

    docker build -t taskflow-demo-app .
    docker run -p 8000:8000 -v taskflow-data:/data taskflow-demo-app
