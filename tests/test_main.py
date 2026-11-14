"""Contract tests for the demo api-service.

The /health cases pin the exact response shapes from
taskflow-api-docs/api-conventions.md ("Health checks"): any deviation
from those shapes is a contract break, not a style issue. Error cases
pin the top-level "error" envelope from the same document ("Errors").

Every test runs against a throwaway SQLite file (see the fixture), so
the suite never touches the developer's real tasks.db.
"""
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    store = main.TaskStore(str(tmp_path / "tasks.db"))
    # Deployment order: migrations run before the service starts
    # (taskflow-docs/deployment.md), so the fixture migrates first.
    main.migrate(store.db_path)
    monkeypatch.setattr(main, "store", store)
    return store


def test_root_index_is_excluded_from_openapi():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {
        "service": "taskflow-demo-app",
        "description": "Demo implementation of the TaskFlow api-service. Fictional data only.",
        "links": {
            "health": "/health",
            "docs": "/docs",
            "tasks": "/v1/tasks",
            "openapi": "/openapi.json",
        },
    }
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/" not in schema.json()["paths"]


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_public_docs_cors_preflight_is_logged(capsys):
    r = client.options("/v1/tasks", headers={
        "Origin": "https://docs.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
        "X-Request-ID": "docs-preflight",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-headers"] == "Content-Type"
    assert r.headers["x-request-id"] == "docs-preflight"
    assert '"request_id": "docs-preflight"' in capsys.readouterr().out


@pytest.mark.parametrize("path,status", [("/health", 200), ("/v1/tasks/999999", 404)])
def test_public_docs_can_read_responses(path, status):
    r = client.get(path, headers={"Origin": "https://docs.example.com"})
    assert r.status_code == status
    assert r.headers["access-control-allow-origin"] == "*"
    assert "x-request-id" in r.headers


def test_health_degraded_when_store_is_down(tmp_path, monkeypatch):
    # A store whose database file cannot be opened at all: the parent
    # directory does not exist, so the real ping() fails for real.
    broken = main.TaskStore(str(tmp_path / "missing-dir" / "tasks.db"))
    monkeypatch.setattr(main, "store", broken)
    r = client.get("/health")
    assert r.status_code == 503
    # Exact contract shape: no version, host, or extra detail keys.
    assert r.json() == {"status": "degraded", "checks": {"task_store": "down"}}


def test_health_degraded_when_database_file_is_corrupt(tmp_path, monkeypatch):
    # A file that exists but is not a database at all. A bare "SELECT 1"
    # ping would still succeed here; probing the tasks table must not.
    db = tmp_path / "tasks.db"
    db.write_bytes(b"this is not a sqlite database")
    monkeypatch.setattr(main, "store", main.TaskStore(str(db)))
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "checks": {"task_store": "down"}}


def test_task_routes_use_error_envelope_when_store_is_down(tmp_path, monkeypatch):
    broken = main.TaskStore(str(tmp_path / "missing-dir" / "tasks.db"))
    monkeypatch.setattr(main, "store", broken)
    expected = {
        "error": {"code": "store_unavailable", "message": "task store is unavailable"}
    }
    for r in (
        client.get("/v1/tasks"),
        client.post("/v1/tasks", json={"title": "x"}),
        client.get("/v1/tasks/1"),
    ):
        assert r.status_code == 503
        assert r.json() == expected


def test_tasks_survive_a_new_store_instance(fresh_store):
    # The point of the SQLite backing: data persists across store
    # instances (and, in production, across process restarts).
    created = fresh_store.create("persist me", "todo")
    reopened = main.TaskStore(fresh_store.db_path)
    assert reopened.get(created["id"])["title"] == "persist me"


def test_task_round_trip():
    r = client.post("/v1/tasks", json={"title": "write tests"})
    assert r.status_code == 201
    task = r.json()
    assert task["title"] == "write tests"
    assert task["status"] == "todo"

    r = client.get(f"/v1/tasks/{task['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == task["id"]


def test_list_tasks_paginates_with_cursor():
    for i in range(3):
        client.post("/v1/tasks", json={"title": f"t{i}"})
    page1 = client.get("/v1/tasks", params={"limit": 2}).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] == page1["items"][-1]["id"]
    page2 = client.get(
        "/v1/tasks", params={"cursor": page1["next_cursor"], "limit": 2}
    ).json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None


def test_list_tasks_filters_by_status_and_pages():
    # TF-102: a board column must be pageable on its own, in id order,
    # without downloading the other columns.
    for title, status in (
        ("a", "todo"),
        ("b", "doing"),
        ("c", "doing"),
        ("d", "done"),
        ("e", "doing"),
    ):
        client.post("/v1/tasks", json={"title": title, "status": status})
    page1 = client.get(
        "/v1/tasks", params={"status": "doing", "limit": 2}
    ).json()
    assert [t["title"] for t in page1["items"]] == ["b", "c"]
    assert page1["next_cursor"] == page1["items"][-1]["id"]
    page2 = client.get(
        "/v1/tasks",
        params={"status": "doing", "limit": 2, "cursor": page1["next_cursor"]},
    ).json()
    assert [t["title"] for t in page2["items"]] == ["e"]
    assert page2["next_cursor"] is None


def test_list_tasks_rejects_invalid_status_filter():
    r = client.get("/v1/tasks", params={"status": "bogus"})
    assert r.status_code == 422
    assert r.json() == {
        "error": {
            "code": "invalid_status",
            "message": "status must be todo, doing or done",
        }
    }


def test_list_tasks_clamps_out_of_range_pagination():
    client.post("/v1/tasks", json={"title": "only"})
    r = client.get("/v1/tasks", params={"limit": 0, "cursor": -5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1  # limit clamped up to 1
    assert body["next_cursor"] is None


def test_status_index_exists(fresh_store):
    # The (status, id) index is what keeps a filtered page from walking
    # the whole table; the fixture's migration must have created it.
    with fresh_store._connect() as conn:
        names = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(tasks)").fetchall()
        }
    assert "idx_tasks_status_id" in names


def test_migration_is_idempotent_on_a_populated_database(fresh_store):
    # The pre-start migration runs on every deploy: running it again
    # against live data must change nothing.
    created = fresh_store.create("keep me", "doing")
    main.migrate(fresh_store.db_path)
    main.migrate(fresh_store.db_path)
    assert fresh_store.get(created["id"])["title"] == "keep me"


def test_health_degraded_when_database_is_not_migrated(tmp_path, monkeypatch):
    # The deployment failure this design exists to surface: the file
    # is there and openable, but migrate.py never ran, so there is no
    # tasks table to probe. Degraded, per the health contract - the
    # store must not silently migrate mid-request.
    unmigrated = main.TaskStore(str(tmp_path / "unmigrated.db"))
    monkeypatch.setattr(main, "store", unmigrated)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "checks": {"task_store": "down"}}


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
