"""Worker queue contract: claims, sanitized failures, and digest integration."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import main
import worker
from digest import OutboxSender


@pytest.fixture
def store(tmp_path):
    store = main.TaskStore(str(tmp_path / "tasks.db"))
    main.migrate(store.db_path)
    return store


def job(store, job_id):
    with store._connect() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def test_schema_is_prestart_and_idempotent(store, tmp_path):
    job_id = worker.enqueue(store, "test", {}, now=10)
    main.migrate(store.db_path)
    assert set(job(store, job_id)) == {
        "id", "kind", "payload", "status", "attempts", "next_attempt_at",
        "request_id", "last_error", "created_at", "updated_at",
    }
    assert job(store, job_id)["attempts"] == 0
    with pytest.raises(main.sqlite3.OperationalError):
        worker.enqueue(str(tmp_path / "unmigrated.db"), "test", {})


@pytest.mark.parametrize("payload", [
    [], None, {"email": "fictional@example.invalid"}, {"id": []}, {"id": {}},
    {"id": float("nan")}, {"id": float("inf")},
])
def test_enqueue_rejects_invalid_payload(store, payload):
    with pytest.raises(ValueError):
        worker.enqueue(store, "test", payload)


def test_valid_payload_and_success(store, capsys):
    payload = {"id": 1, "week": "private-week", "ratio": 1.5, "yes": True, "other": None}
    job_id = worker.enqueue(store.db_path, "test", payload, "edge-123", now=10)
    assert job(store, job_id)["status"] == "queued"
    assert job(store, job_id)["next_attempt_at"] == 10
    seen = []
    assert worker.process_due(store, {"test": seen.append}, 10) == 1
    assert seen == [payload]
    assert job(store, job_id)["status"] == "done"
    assert worker.process_due(store, {}, 100) == 0
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert [r["event"] for r in records] == ["claimed", "done"]
    for record in records:
        assert set(record) == {
            "ts", "service", "level", "request_id", "job_id", "kind", "event",
        }
        assert record["request_id"] == "edge-123"
        assert record["job_id"] == job_id
        assert record["service"] == "worker"
    assert "private-week" not in output


def test_five_failures_back_off_then_stay_dead(store, capsys):
    def fail(payload):
        raise RuntimeError("private exception content")

    job_id = worker.enqueue(store, "test", {}, now=10)
    now = 10
    for attempt in range(1, 6):
        assert worker.process_due(store, {"test": fail}, now) == 1
        row = job(store, job_id)
        assert row["attempts"] == attempt
        assert row["last_error"] == "RuntimeError"
        assert row["status"] == ("dead" if attempt == 5 else "queued")
        assert row["next_attempt_at"] == now + min(2 ** attempt, 300)
        assert row["updated_at"] == now
        assert worker.process_due(store, {"test": fail}, now) == 0
        now = row["next_attempt_at"]
    assert worker.process_due(store, {"test": fail}, now + 1000) == 0
    output = capsys.readouterr().out
    assert "private exception content" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert [r["event"] for r in records].count("retry") == 4
    assert records[-1]["event"] == "dead"
    assert records[-1]["request_id"] is None


def test_unknown_kind_is_failure(store):
    job_id = worker.enqueue(store, "unknown", {}, now=0)
    assert worker.process_due(store, {}, 0) == 1
    assert job(store, job_id)["last_error"] == "KeyError"


@pytest.mark.parametrize("attempts,expected", [(0, 1), (1, 2), (5, 32), (9, 300), (20, 300)])
def test_backoff_cap(attempts, expected):
    assert worker.backoff_seconds(attempts) == expected


def test_due_order_and_future_jobs(store):
    worker.enqueue(store, "test", {"id": 2}, now=20)
    worker.enqueue(store, "test", {"id": 1}, now=10)
    worker.enqueue(store, "test", {"id": 3}, now=30)
    seen = []
    assert worker.process_due(store, {"test": seen.append}, 20) == 2
    assert seen == [{"id": 1}, {"id": 2}]


def test_concurrent_drains_claim_once(store):
    for number in range(10):
        worker.enqueue(store, "test", {"id": number}, now=0)
    seen = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(
            lambda _: worker.process_due(store, {"test": seen.append}, 0), range(2)
        ))
    assert sum(counts) == 10
    assert sorted(p["id"] for p in seen) == list(range(10))


def test_weekly_digest_uses_outbox_and_ledger(store, tmp_path):
    store.create("fictional task", "done")
    outbox = tmp_path / "outbox.jsonl"
    handlers = worker.default_handlers(store, sender=OutboxSender(str(outbox)))
    for _ in range(2):
        worker.enqueue(store, "weekly_digest", {"user_id": "user-1", "week": "2026-W46"}, now=0)
    assert worker.process_due(store, handlers, 0) == 2
    delivery, = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert delivery["key"] == "user-1:2026-W46"
    assert "done: 1" in delivery["body"]
    with store._connect() as conn:
        row = conn.execute("SELECT * FROM digest_sends").fetchone()
    assert row["status"] == "sent"
    assert row["attempts"] == 1


def test_cli_uses_environment_database_and_default_outbox(store, tmp_path):
    worker.enqueue(store, "weekly_digest", {"user_id": "user-1", "week": "2026-W46"}, now=0)
    result = subprocess.run(
        [sys.executable, "scripts/run_worker.py"],
        env=dict(os.environ, TASKFLOW_DB_PATH=store.db_path),
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"processed": 1}
    assert (tmp_path / "digest-outbox.jsonl").exists()
