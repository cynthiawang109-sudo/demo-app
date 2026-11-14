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


@pytest.fixture(autouse=True)
def test_payload_schema(monkeypatch):
    monkeypatch.setitem(worker.PAYLOAD_SCHEMAS, "test", lambda payload: None)


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


@pytest.mark.parametrize("user_context", [{}, {"user_id": 42}])
def test_valid_payload_and_success(store, capsys, user_context):
    payload = {"id": 1, "week": "private-week", "ratio": 1.5, "yes": True, "other": None}
    payload.update(user_context)
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
        } | set(user_context)
        if user_context:
            assert record["user_id"] == 42
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
        assert worker.process_due(store, {"test": fail}, now, clock=lambda: now) == 1
        row = job(store, job_id)
        assert row["attempts"] == attempt
        assert row["last_error"] == "RuntimeError"
        assert row["status"] == ("dead" if attempt == 5 else "queued")
        assert row["next_attempt_at"] == now + min(2 ** attempt, 300)
        assert row["updated_at"] == now
        assert worker.process_due(store, {"test": fail}, now, clock=lambda: now) == 0
        now = row["next_attempt_at"]
    assert worker.process_due(store, {"test": fail}, now + 1000) == 0
    output = capsys.readouterr().out
    assert "private exception content" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert [r["event"] for r in records].count("retry") == 4
    assert records[-1]["event"] == "dead"
    assert records[-1]["request_id"] is None


def test_unknown_kind_is_rejected(store):
    with pytest.raises(ValueError, match="unsupported job kind"):
        worker.enqueue(store, "unknown", {}, now=0)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


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


@pytest.mark.parametrize("user_id", [42, "user-1"])
def test_weekly_digest_uses_outbox_and_ledger(store, tmp_path, user_id):
    store.create("fictional task", "done")
    outbox = tmp_path / "outbox.jsonl"
    handlers = worker.default_handlers(store, sender=OutboxSender(str(outbox)))
    for _ in range(2):
        worker.enqueue(store, "weekly_digest", {"user_id": user_id, "week": "2026-W46"}, now=0)
    assert worker.process_due(store, handlers, 0) == 2
    delivery, = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert delivery["key"] == f"{user_id}:2026-W46"
    assert delivery["user_id"] == user_id
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


@pytest.mark.parametrize("use_store", [True, False])
def test_interrupted_job_recovers_and_completes(store, use_store):
    job_id = worker.enqueue(store, "test", {"id": 1}, now=0)
    untouched = worker.enqueue(store, "test", {}, now=100)
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    target = store if use_store else store.db_path
    assert worker.recover_interrupted(target, clock=lambda: 10) == 1
    row = job(store, job_id)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "queued", 1, "InterruptedError",
    )
    assert row["next_attempt_at"] == 12
    assert row["updated_at"] == 10
    assert job(store, untouched)["attempts"] == 0
    assert worker.recover_interrupted(target, clock=lambda: 11) == 0
    seen = []
    assert worker.process_due(store, {"test": seen.append}, 11) == 0
    assert worker.process_due(store, {"test": seen.append}, 12) == 1
    assert seen == [{"id": 1}]
    assert job(store, job_id)["status"] == "done"
    assert job(store, job_id)["last_error"] is None


def test_interrupted_fifth_attempt_is_dead_lettered(store):
    job_id = worker.enqueue(store, "test", {}, now=0)
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 4 WHERE id = ?", (job_id,),
        )
    assert worker.recover_interrupted(store, clock=lambda: 10) == 1
    row = job(store, job_id)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "dead", 5, "InterruptedError",
    )
    assert row["next_attempt_at"] == 42
    assert row["updated_at"] == 10
    assert worker.process_due(store, {}, 1000) == 0


@pytest.mark.parametrize("user_id", [
    42, "demo-user", "user-1", "User_2", "a" * 64,
])
def test_weekly_digest_lifecycle_user_context(store, capsys, user_id):
    def fail(payload):
        raise RuntimeError("private failure")

    payload = {"week": "2026-W46"}
    if user_id is not None:
        payload["user_id"] = user_id
    worker.enqueue(store, "weekly_digest", payload, now=0)
    now = 0
    for attempt in range(1, 6):
        worker.process_due(store, {"weekly_digest": fail}, now, clock=lambda: now)
        now += worker.backoff_seconds(attempt)
    worker.enqueue(store, "weekly_digest", payload, now=now)
    worker.process_due(store, {"weekly_digest": lambda payload: None}, now)
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert {r["event"] for r in records} == {"claimed", "done", "retry", "dead"}
    for record in records:
        if type(user_id) is int or user_id in ("demo-user", "user-1", "User_2", "a" * 64):
            assert record["user_id"] == user_id
        else:
            assert "user_id" not in record
        assert "week" not in record
    assert "private" not in output
    assert "fictional@example.invalid" not in output


def test_cli_recovers_interrupted_jobs_at_startup(store):
    job_id = worker.enqueue(store, "test", {}, now=0)
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    result = subprocess.run(
        [sys.executable, "scripts/run_worker.py"],
        env=dict(os.environ, TASKFLOW_DB_PATH=store.db_path),
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"processed": 0}
    row = job(store, job_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["last_error"] == "InterruptedError"


def test_slow_failure_schedules_from_failure_time_and_preserves_cutoff(store):
    current_time = 10

    def fail(payload):
        nonlocal current_time
        current_time = 30
        raise RuntimeError("slow failure")

    job_id = worker.enqueue(store, "test", {}, now=10)
    future_id = worker.enqueue(store, "test", {}, now=20)
    assert worker.process_due(store, {"test": fail}, 10, clock=lambda: current_time) == 1
    row = job(store, job_id)
    assert row["attempts"] == 1
    assert row["next_attempt_at"] == 32
    assert row["updated_at"] == 30
    assert job(store, future_id)["attempts"] == 0
    assert job(store, future_id)["status"] == "queued"


def test_recovery_reads_clock_for_each_interrupted_job(store):
    job_ids = [worker.enqueue(store, "test", {}, now=0) for _ in range(2)]
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running'")
    times = iter([30, 40])
    assert worker.recover_interrupted(store, clock=lambda: next(times)) == 2
    rows = [job(store, job_id) for job_id in job_ids]
    assert sorted(row["updated_at"] for row in rows) == [30, 40]
    assert sorted(row["next_attempt_at"] for row in rows) == [32, 42]


@pytest.mark.parametrize("payload", [
    {"user_id": "fictional@example.invalid", "week": "2026-W46"},
    {"user_id": True, "week": "2026-W46"},
    {"user_id": 1.5, "week": "2026-W46"},
    {"user_id": None, "week": "2026-W46"},
    *[{"user_id": value, "week": "2026-W46"}
      for value in ("", "a" * 65, "private user", "user\n")],
    {"user_id": 42}, {"week": "2026-W46"}, {},
    *[{"user_id": 42, "week": value}
      for value in (None, 202646, "2026-W6", "2026-W460", "2026-W46\n",
                    "fictional@example.invalid")],
    {"user_id": 42, "week": "2026-W46", "name": "private person"},
    {"user_id": 42, "week": "2026-W46", "contact": "fictional@example.invalid"},
])
def test_digest_schema_rejects_before_persistence(store, payload):
    with pytest.raises(ValueError):
        worker.enqueue(store, "weekly_digest", payload)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_legacy_email_payload_fails_without_dispatch_or_leak(store, capsys, caplog):
    email = "fictional@example.invalid"
    with store._connect() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (kind, payload, status, next_attempt_at, created_at) "
            "VALUES (?, ?, 'queued', 0, 0)",
            ("weekly_digest", json.dumps({"user_id": email, "week": "2026-W46"})),
        )
        job_id = cursor.lastrowid
    handlers = worker.default_handlers(store)
    called = []
    original = handlers["weekly_digest"]

    def track(payload):
        called.append(payload)
        return original(payload)

    handlers["weekly_digest"] = track
    with caplog.at_level("INFO"):
        assert worker.process_due(store, handlers, 0, clock=lambda: 0) == 1
    assert called == []
    row = job(store, job_id)
    assert (row["status"], row["attempts"], row["last_error"]) == (
        "queued", 1, "ValueError",
    )
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM digest_sends").fetchone()[0] == 0
    captured = capsys.readouterr()
    assert email not in captured.out + captured.err + caplog.text
