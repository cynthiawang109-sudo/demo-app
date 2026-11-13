"""TF-109; taskflow-docs/observability.md: JSON logs, IDs, and no PII."""

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    store = main.TaskStore(str(tmp_path / "tasks.db"))
    # taskflow-docs/deployment.md: migrate before serving requests.
    main.migrate(store.db_path)
    monkeypatch.setattr(main, "store", store)
    return store


def access_records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_health_log_fields_and_response_id(capsys):
    before = datetime.now(timezone.utc)
    response = client.get("/health")
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    record, = access_records(capsys)
    assert set(record) == {
        "ts", "service", "level", "request_id", "method", "path", "status",
        "duration_ms",
    }
    assert isinstance(record["ts"], str)
    timestamp = datetime.fromisoformat(record["ts"])
    assert timestamp.utcoffset() == timezone.utc.utcoffset(timestamp)
    assert before <= timestamp <= after
    assert record["service"] == "demo-app"
    assert record["level"] == "info"
    assert isinstance(record["request_id"], str)
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["request_id"]
    assert record["method"] == "GET"
    assert record["path"] == "/health"
    assert type(record["status"]) is int
    assert record["status"] == 200
    assert type(record["duration_ms"]) in (int, float)
    assert record["duration_ms"] >= 0


def test_requests_mint_distinct_ids(capsys):
    responses = [client.get("/health"), client.get("/health")]
    records = access_records(capsys)
    assert len(records) == 2
    ids = [response.headers["X-Request-ID"] for response in responses]
    assert ids[0] != ids[1]
    assert [record["request_id"] for record in records] == ids


def test_inbound_request_id_is_propagated(capsys):
    request_id = "fictional-edge-request-109"
    response = client.get("/health", headers={"x-ReQuEsT-iD": request_id})
    record, = access_records(capsys)
    assert response.headers["X-Request-ID"] == request_id
    assert record["request_id"] == request_id


@pytest.mark.parametrize("path", ["/missing-route", "/v1/tasks/999999"])
def test_not_found_is_logged(capsys, path):
    response = client.get(path)
    record, = access_records(capsys)
    assert response.status_code == record["status"] == 404
    assert record["level"] == "info"
    assert record["path"] == path
    assert record["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/health", "/v1/tasks"])
def test_down_store_logs_503_at_error_level(tmp_path, monkeypatch, capsys, path):
    # Existing down-store pattern: a missing parent makes SQLite fail.
    broken = main.TaskStore(str(tmp_path / "missing-dir" / "tasks.db"))
    monkeypatch.setattr(main, "store", broken)
    response = client.get(path)
    record, = access_records(capsys)
    assert response.status_code == record["status"] == 503
    assert record["level"] == "error"
    assert record["request_id"] == response.headers["X-Request-ID"]


def test_validation_log_excludes_query_and_other_headers(capsys):
    query_value = "fictional-private-query-109"
    header_value = "fictional-private-header-109"
    response = client.get(
        "/v1/tasks", params={"limit": query_value},
        headers={"Authorization": header_value},
    )
    output = capsys.readouterr().out
    record, = [json.loads(line) for line in output.splitlines()]
    assert response.status_code == record["status"] == 422
    assert record["level"] == "info"
    assert record["path"] == "/v1/tasks"
    assert query_value not in output
    assert header_value not in output
    assert "Authorization" not in output


def test_task_title_and_comment_body_never_appear_in_stdout(capsys):
    # observability.md: user content must never enter logs, from either
    # incoming bodies or responses that return stored task/comment text.
    title = f"fictional-private-task-{uuid4()}"
    body = f"fictional-private-comment-{uuid4()}"
    task = client.post("/v1/tasks", json={"title": title})
    assert task.status_code == 201
    task_url = f"/v1/tasks/{task.json()['id']}"
    comment = client.post(f"{task_url}/comments", json={"body": body})
    assert comment.status_code == 201
    for path in (
        "/v1/tasks", task_url, f"{task_url}/comments",
        f"{task_url}/comments/{comment.json()['id']}",
    ):
        assert client.get(path).status_code == 200
    output = capsys.readouterr().out
    assert title not in output
    assert body not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 6
    assert [record["method"] for record in records] == ["POST", "POST"] + ["GET"] * 4


def test_middleware_forwards_stream_chunks_immediately(capsys):
    sent = []

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert sent[-1]["body"] == b"first"
        await send({"type": "http.response.body", "body": b"last"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(main.AccessLogMiddleware(downstream)(
        {"type": "http", "method": "GET", "path": "/stream", "headers": []},
        receive, send,
    ))
    assert [message["type"] for message in sent] == [
        "http.response.start", "http.response.body", "http.response.body",
    ]
    assert sent[-1]["body"] == b"last"
    record, = access_records(capsys)
    assert record["request_id"].encode() == dict(sent[0]["headers"])[b"x-request-id"]
