"""One-shot SQLite job drain; schema is applied by main.migrate before start.

Unlike the production worker service's GET /health, this demo is not a
service: its exit code and summary line are its health signal.
"""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import digest

# Deliberately excludes email addresses and arbitrary payload text.
SAFE_USER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

JOBS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs ("
    "id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, "
    "status TEXT CHECK (status IN ('queued', 'running', 'done', 'dead')), "
    "attempts INTEGER DEFAULT 0, next_attempt_at REAL, request_id TEXT NULL, "
    "last_error TEXT NULL, created_at REAL, updated_at REAL)"
)


def _db_path(store_or_db):
    return getattr(store_or_db, "db_path", store_or_db)


def _validate_weekly_digest(payload):
    if set(payload) != {"user_id", "week"}:
        raise ValueError("weekly_digest requires exactly user_id and week")
    user_id = payload["user_id"]
    if not (type(user_id) is int or (
        isinstance(user_id, str) and SAFE_USER_ID.fullmatch(user_id)
    )):
        raise ValueError("weekly_digest requires a safe user identifier")
    week = payload["week"]
    if not isinstance(week, str) or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", week):
        raise ValueError("weekly_digest requires a week in YYYY-Www format")


PAYLOAD_SCHEMAS = {"weekly_digest": _validate_weekly_digest}


def _validate_payload(kind, payload):
    if not isinstance(payload, dict) or "email" in payload:
        raise ValueError("payload must be an object without an email key")
    if any(not isinstance(key, str) for key in payload) or any(
        type(value) not in (int, float, str, bool, type(None))
        for value in payload.values()
    ):
        raise ValueError("payload must have string keys and JSON scalar values")
    if any(isinstance(value, str) and "@" in value for value in payload.values()):
        raise ValueError("payload values must not contain email addresses")
    validator = PAYLOAD_SCHEMAS.get(kind)
    if validator is None:
        raise ValueError("unsupported job kind")
    validator(payload)


def enqueue(store_or_db, kind, payload, request_id=None, now=None) -> int:
    """Queue a payload validated against its registered job schema."""
    _validate_payload(kind, payload)
    encoded = json.dumps(payload, allow_nan=False)
    now = time.time() if now is None else now
    conn = sqlite3.connect(_db_path(store_or_db))
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO jobs (kind, payload, status, next_attempt_at, "
                "request_id, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
                (kind, encoded, now, request_id, now, now),
            )
        return cursor.lastrowid
    finally:
        conn.close()


def backoff_seconds(attempts):
    return min(2 ** attempts, 300)


def recover_interrupted(db_path_or_store, clock=time.time):
    """Recover running jobs only when no other worker process is active."""
    conn = sqlite3.connect(_db_path(db_path_or_store))
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs = conn.execute(
                "SELECT id, attempts FROM jobs WHERE status = 'running'"
            ).fetchall()
            for job_id, previous_attempts in jobs:
                attempts = previous_attempts + 1
                now = clock()
                conn.execute(
                    "UPDATE jobs SET status = ?, attempts = ?, last_error = ?, "
                    "next_attempt_at = ?, updated_at = ? WHERE id = ?",
                    ("dead" if attempts >= 5 else "queued", attempts,
                     "InterruptedError", now + backoff_seconds(attempts), now, job_id),
                )
        return len(jobs)
    finally:
        conn.close()


def _log(job, event):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": "worker",
        "level": "error" if event == "dead" else "warning" if event == "retry" else "info",
        "request_id": job["request_id"],
        "job_id": job["id"],
        "kind": job["kind"],
        "event": event,
    }
    try:
        payload = json.loads(job["payload"])
    except (ValueError, TypeError):
        payload = None
    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    if type(user_id) is int or (
        isinstance(user_id, str) and SAFE_USER_ID.fullmatch(user_id)
    ):
        record["user_id"] = user_id
    print(json.dumps(record), flush=True)


def process_due(db_path_or_store, handlers, now, clock=time.time) -> int:
    """Atomically claim oldest due jobs, dispatch outside the write transaction."""
    conn = sqlite3.connect(_db_path(db_path_or_store))
    conn.row_factory = sqlite3.Row
    processed = 0
    try:
        while True:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                job = conn.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' AND next_attempt_at <= ? "
                    "ORDER BY created_at, id LIMIT 1", (now,),
                ).fetchone()
                if job is None:
                    return processed
                conn.execute(
                    "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                    (now, job["id"]),
                )
            _log(job, "claimed")
            try:
                payload = json.loads(job["payload"])
                _validate_payload(job["kind"], payload)
                handlers[job["kind"]](payload)
            except Exception as exc:  # noqa: BLE001 - handler failures become retry data
                failure_time = clock()
                attempts = job["attempts"] + 1
                event = "dead" if attempts >= 5 else "retry"
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = ?, attempts = ?, last_error = ?, "
                        "next_attempt_at = ?, updated_at = ? WHERE id = ?",
                        ("dead" if event == "dead" else "queued", attempts,
                         type(exc).__name__, failure_time + backoff_seconds(attempts),
                         failure_time, job["id"]),
                    )
            else:
                event = "done"
                with conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'done', last_error = NULL, "
                        "updated_at = ? WHERE id = ?", (now, job["id"]),
                    )
            processed += 1
            _log(job, event)
    finally:
        conn.close()


def default_handlers(db_path_or_store, sender=None):
    """Use the same single-user send path and outbox as scripts/run_digest.py."""
    db_path = _db_path(db_path_or_store)
    if sender is None:
        sender = digest.OutboxSender(str(
            Path(db_path).resolve().parent / "digest-outbox.jsonl"
        ))

    def weekly_digest(payload):
        return digest.run_digest(db_path, payload["user_id"], payload["week"], sender)

    return {"weekly_digest": weekly_digest}
