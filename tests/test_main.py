"""Contract tests for the demo api-service.

The /health cases pin the exact response shapes from
taskflow-api-docs/api-conventions.md ("Health checks"): any deviation
from those shapes is a contract break, not a style issue.
"""
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_degraded_when_store_is_down(monkeypatch):
    def boom():
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(main.store, "ping", boom)
    r = client.get("/health")
    assert r.status_code == 503
    # Exact contract shape: no version, host, or extra detail keys.
    assert r.json() == {"status": "degraded", "checks": {"task_store": "down"}}


def test_task_round_trip():
    r = client.post("/v1/tasks", json={"title": "write tests"})
    assert r.status_code == 201
    task = r.json()
    assert task["title"] == "write tests"
    assert task["status"] == "todo"

    r = client.get(f"/v1/tasks/{task['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == task["id"]


def test_get_missing_task_uses_contract_error_shape():
    # Contract ("Errors" in api-conventions.md): a top-level "error"
    # object - NOT wrapped in FastAPI's "detail".
    r = client.get("/v1/tasks/999999")
    assert r.status_code == 404
    assert r.json() == {
        "error": {"code": "task_not_found", "message": "task 999999 does not exist"}
    }


def test_create_task_rejects_invalid_status():
    r = client.post("/v1/tasks", json={"title": "x", "status": "bogus"})
    assert r.status_code == 422
    assert r.json() == {
        "error": {
            "code": "invalid_status",
            "message": "status must be todo, doing or done",
        }
    }
