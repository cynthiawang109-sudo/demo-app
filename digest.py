"""Weekly digest job — the demo slice of the worker component.

Fixes TF-103. The old job retried silently and could deliver twice:

- Every attempt is recorded in the `digest_sends` ledger (attempt count
  and a sanitized error code) and logged with the user ID — never the
  email address (PII rule, `taskflow-api-docs/data-model.md`). Only the
  failure's error code (exception class name) is stored or logged: an
  exception message can carry message content or addresses, so it is
  never persisted (PII in logs is a severity-1, incident-runbook.md).
- Sends are idempotent per (user_id, week): that pair is the ledger's
  primary key and the idempotency key handed to the sender. A `sent`
  row is never re-driven, and the key covers the crash window between
  "the sender delivered" and "the success was recorded" — the sender
  contract is to dedupe on that key, atomically (see `OutboxSender`).
- The retry ceiling is 5 attempts in total, across runs, with bounded
  exponential backoff between attempts; then the row is dead-lettered
  (`status = 'dead'`) and later runs leave it alone until a human
  resets it (system-overview.md; reset steps in incident-runbook.md).

The ledger table is created by the pre-start migration (main.migrate),
not lazily here — same rule as the board index (deployment.md section 4).
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("taskflow.digest")

MAX_ATTEMPTS = 3  # retry ceiling before dead-letter (system-overview.md)
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0

# Ledger schema. Lives here because the worker owns this table;
# main.migrate() applies it as part of the pre-start migration.
DIGEST_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS digest_sends ("
    "user_id TEXT NOT NULL, "
    "week TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, "
    "last_error TEXT, "
    "sent_at TEXT, "
    "PRIMARY KEY (user_id, week))"
)


def compose_digest(conn: sqlite3.Connection, week: str) -> str:
    """Deterministic digest body: task counts by status. Same inputs,
    same body — safe to regenerate on every retry."""
    counts = {"todo": 0, "doing": 0, "done": 0}
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM tasks GROUP BY status"
    ):
        if status in counts:
            counts[status] = n
    total = sum(counts.values())
    return (
        f"TaskFlow weekly digest ({week})\n\n"
        f"todo: {counts['todo']}\n"
        f"doing: {counts['doing']}\n"
        f"done: {counts['done']}\n"
        f"total: {total}"
    )


def run_digest(db_path: str, user_id: str, week: str, sender,
               max_attempts: int = MAX_ATTEMPTS,
               sleeper=time.sleep) -> dict:
    """Drive one (user_id, week) digest to a terminal state.

    `sender` is a callable taking (user_id, week, body,
    idempotency_key); it must dedupe deliveries on the idempotency
    key, atomically across processes. `sleeper` is injectable so tests
    can verify the backoff schedule without waiting. Returns a dict
    with the outcome: 'sent', 'already_sent', or 'dead_lettered',
    plus the total attempt count.

    Overlapping runs against the same database are safe: attempt
    increments are SQL-side (never a read-modify-write), and the
    sender's key dedupe is the delivery guard.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT OR IGNORE INTO digest_sends"
            " (user_id, week, status, attempts)"
            " VALUES (?, ?, 'pending', 0)",
            (user_id, week),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, attempts FROM digest_sends"
            " WHERE user_id = ? AND week = ?",
            (user_id, week),
        ).fetchone()
        if row["status"] == "sent":
            log.info("digest already sent for user %s week %s",
                     user_id, week)
            return {"outcome": "already_sent", "attempts": row["attempts"]}
        if row["status"] == "dead":
            log.warning("digest for user %s week %s is dead-lettered;"
                        " not retrying", user_id, week)
            return {"outcome": "dead_lettered",
                    "attempts": row["attempts"]}

        attempts = row["attempts"]
        idempotency_key = f"{user_id}:{week}"
        body = compose_digest(conn, week)
        while attempts < max_attempts:
            # Reserve the attempt BEFORE sending, SQL-side so
            # overlapping runs cannot lose an increment; a crash
            # mid-send then leaves a truthful count.
            conn.execute(
                "UPDATE digest_sends SET attempts = attempts + 1"
                " WHERE user_id = ? AND week = ?",
                (user_id, week),
            )
            conn.commit()
            attempts = conn.execute(
                "SELECT attempts FROM digest_sends"
                " WHERE user_id = ? AND week = ?",
                (user_id, week),
            ).fetchone()["attempts"]
            log.info("digest attempt %d/%d for user %s week %s",
                     attempts, max_attempts, user_id, week)
            try:
                sender(user_id=user_id, week=week, body=body,
                       idempotency_key=idempotency_key)
            except Exception as exc:  # noqa: BLE001 - sender failure is data, not silence
                # Store and log only the error code (exception class
                # name) — never the message, which can carry PII.
                error_code = type(exc).__name__
                conn.execute(
                    "UPDATE digest_sends SET last_error = ?"
                    " WHERE user_id = ? AND week = ?",
                    (error_code, user_id, week),
                )
                conn.commit()
                if attempts < max_attempts:
                    delay = min(
                        BACKOFF_BASE_SECONDS * 2 ** (attempts - 1),
                        BACKOFF_CAP_SECONDS)
                    log.warning(
                        "digest attempt %d for user %s week %s failed"
                        " (%s); retrying in %.1fs",
                        attempts, user_id, week, error_code, delay)
                    sleeper(delay)
                else:
                    log.warning(
                        "digest attempt %d for user %s week %s failed"
                        " (%s); no attempts left",
                        attempts, user_id, week, error_code)
                continue
            conn.execute(
                "UPDATE digest_sends SET status = 'sent', sent_at = ?,"
                " last_error = NULL"
                " WHERE user_id = ? AND week = ?",
                (datetime.now(timezone.utc).isoformat(), user_id, week),
            )
            conn.commit()
            log.info("digest sent for user %s week %s on attempt %d",
                     user_id, week, attempts)
            return {"outcome": "sent", "attempts": attempts}

        conn.execute(
            "UPDATE digest_sends SET status = 'dead'"
            " WHERE user_id = ? AND week = ?",
            (user_id, week),
        )
        conn.commit()
        log.error("digest dead-lettered for user %s week %s after"
                  " %d attempts", user_id, week, attempts)
        return {"outcome": "dead_lettered", "attempts": attempts}
    finally:
        conn.close()


class OutboxSender:
    """Demo sender honoring the idempotency contract: appends each
    delivery to a JSON-lines outbox file, skipping keys already
    delivered — so a retry after a partial send cannot double-send.
    The check-and-append runs under an OS file lock, so the dedupe is
    atomic across overlapping processes, not just threads."""

    def __init__(self, outbox_path: str) -> None:
        self.outbox_path = Path(outbox_path)

    def __call__(self, user_id: str, week: str, body: str,
                 idempotency_key: str) -> None:
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a+", encoding="utf-8") as fh:
            self._lock(fh)
            try:
                fh.seek(0)
                for line in fh:
                    if line.strip() and \
                            json.loads(line)["key"] == idempotency_key:
                        return  # already delivered under this key
                record = {"key": idempotency_key, "user_id": user_id,
                          "week": week, "body": body}
                fh.write(json.dumps(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                self._unlock(fh)

    @staticmethod
    def _lock(fh) -> None:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock(fh) -> None:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
