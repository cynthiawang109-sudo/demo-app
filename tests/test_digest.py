"""Digest job tests (TF-103): idempotent per (user, week), every
attempt recorded and logged, dead-letter after 5 attempts."""

import logging
import sqlite3

import pytest

import main
from digest import compose_digest, run_digest


class FakeSender:
    """Sender test double honoring the idempotency contract: it
    delivers at most once per key. Modes: 'ok', 'flaky' (fail N times
    then succeed), 'fail' (always raise), 'partial' (deliver, then
    raise once — the ack is lost after a successful send)."""

    def __init__(self, mode="ok", failures=0):
        self.mode = mode
        self.failures = failures
        self.calls = 0
        self.delivered = []

    def __call__(self, user_id, week, body, idempotency_key):
        self.calls += 1
        if self.mode == "fail":
            raise RuntimeError("smtp down")
        if self.mode == "flaky" and self.calls <= self.failures:
            raise RuntimeError("smtp down")
        if idempotency_key not in [d[0] for d in self.delivered]:
            self.delivered.append((idempotency_key, body))
        if self.mode == "partial" and self.calls == 1:
            raise RuntimeError("connection lost after send")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    main.migrate(str(path))
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO tasks (title, status) VALUES (?, ?)",
        [("a", "todo"), ("b", "doing"), ("c", "done"), ("d", "done")],
    )
    conn.commit()
    conn.close()
    return str(path)


def ledger(db, user="user-1", week="2026-W46"):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM digest_sends WHERE user_id = ? AND week = ?",
        (user, week),
    ).fetchone()
    conn.close()
    return row


def test_sends_once_per_user_week(db):
    sender = FakeSender()
    first = run_digest(db, "user-1", "2026-W46", sender)
    second = run_digest(db, "user-1", "2026-W46", sender)
    assert first["outcome"] == "sent"
    assert second["outcome"] == "already_sent"
    assert sender.calls == 1
    assert len(sender.delivered) == 1


def test_retries_are_recorded_and_eventually_send(db, caplog):
    sender = FakeSender(mode="flaky", failures=2)
    with caplog.at_level(logging.INFO, logger="taskflow.digest"):
        result = run_digest(db, "user-1", "2026-W46", sender)
    assert result == {"outcome": "sent", "attempts": 3}
    row = ledger(db)
    assert row["status"] == "sent"
    assert row["attempts"] == 3
    assert row["last_error"] is None  # cleared on success
    assert row["sent_at"] is not None
    # Attempts are visible in the log, keyed by user ID.
    assert "attempt 1/5 for user user-1" in caplog.text
    assert "attempt 3/5 for user user-1" in caplog.text


def test_dead_letters_after_five_attempts_and_stays_dead(db):
    sender = FakeSender(mode="fail")
    result = run_digest(db, "user-1", "2026-W46", sender)
    assert result == {"outcome": "dead_lettered", "attempts": 5}
    row = ledger(db)
    assert row["status"] == "dead"
    assert "smtp down" in row["last_error"]  # failure is not silent
    again = run_digest(db, "user-1", "2026-W46", sender)
    assert again["outcome"] == "dead_lettered"
    assert sender.calls == 5  # no further attempts once dead


def test_partial_send_does_not_double_deliver(db):
    sender = FakeSender(mode="partial")
    result = run_digest(db, "user-1", "2026-W46", sender)
    assert result["outcome"] == "sent"
    assert result["attempts"] == 2
    assert len(sender.delivered) == 1  # deduped on the idempotency key


def test_different_week_sends_again(db):
    sender = FakeSender()
    run_digest(db, "user-1", "2026-W46", sender)
    result = run_digest(db, "user-1", "2026-W47", sender)
    assert result["outcome"] == "sent"
    assert len(sender.delivered) == 2


def test_ledger_has_no_email_column(db):
    conn = sqlite3.connect(db)
    columns = [r[1] for r in
               conn.execute("PRAGMA table_info(digest_sends)")]
    conn.close()
    assert "email" not in columns  # PII rule: log/identify by user ID


def test_compose_digest_counts_by_status(db):
    conn = sqlite3.connect(db)
    body = compose_digest(conn, "2026-W46")
    conn.close()
    assert "todo: 1" in body
    assert "doing: 1" in body
    assert "done: 2" in body
    assert "total: 4" in body
