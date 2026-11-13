"""TF-106: append-only revisions and the TaskFlow error/paging contracts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    store = main.TaskStore(str(tmp_path / "tasks.db"))
    main.migrate(store.db_path)
    monkeypatch.setattr(main, "store", store)
    return store


@pytest.fixture
def task_url(fresh_store):
    task = fresh_store.create("review fictional task", "todo")
    return f"/v1/tasks/{task['id']}/comments"


def assert_error(response, status, code):
    assert response.status_code == status
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def test_create_list_get(task_url):
    assert client.get(task_url).json() == {"items": [], "next_cursor": None}
    response = client.post(task_url, json={"body": "  original text  "})
    assert response.status_code == 201
    comment = response.json()
    assert comment == {
        "id": 1, "task_id": 1, "body": "  original text  ", "supersedes_id": None,
    }
    response = client.get(f"{task_url}/{comment['id']}")
    assert response.status_code == 200
    assert response.json() == comment
    response = client.get(task_url)
    assert response.status_code == 200
    assert response.json() == {"items": [comment], "next_cursor": None}


def test_revision_chain_preserves_order_and_originals(task_url):
    revisions = []
    previous = None
    for body in ("v1", "v2", "v3"):
        response = client.post(
            task_url, json={"body": body, "supersedes_id": previous}
        )
        assert response.status_code == 201
        revision = response.json()
        assert revision["supersedes_id"] == previous
        revisions.append(revision)
        previous = revision["id"]
    assert client.get(task_url).json() == {"items": revisions, "next_cursor": None}
    assert [r["id"] for r in revisions] == sorted(r["id"] for r in revisions)
    for revision in revisions:
        assert client.get(f"{task_url}/{revision['id']}").json() == revision


def test_stale_supersede_conflicts_without_appending(task_url):
    original = client.post(task_url, json={"body": "v1"}).json()
    revision = {"body": "v2", "supersedes_id": original["id"]}
    assert client.post(task_url, json=revision).status_code == 201
    assert_error(client.post(task_url, json=revision), 409, "conflict")
    assert len(client.get(task_url).json()["items"]) == 2


def test_cross_task_supersede_and_get_hide_comment(task_url, fresh_store):
    other = fresh_store.create("other fictional task", "todo")
    comment = fresh_store.create_comment(other["id"], "other comment")
    # A cross-task id must be indistinguishable from an unknown id.
    unknown = client.post(task_url, json={"body": "x", "supersedes_id": 999999})
    cross = client.post(
        task_url, json={"body": "x", "supersedes_id": comment["id"]}
    )
    assert_error(cross, 404, "comment_not_found")
    assert cross.json() == unknown.json()
    assert_error(client.get(f"{task_url}/{comment['id']}"), 404, "comment_not_found")
    assert client.get(task_url).json()["items"] == []


def test_supersede_nonexistent_comment(task_url):
    response = client.post(task_url, json={"body": "x", "supersedes_id": 999999})
    assert_error(response, 404, "comment_not_found")
    assert client.get(task_url).json()["items"] == []


@pytest.mark.parametrize("route", ["create", "list", "get"])
def test_missing_task(route):
    url = "/v1/tasks/999999/comments"
    if route == "create":
        response = client.post(url, json={"body": "x"})
    else:
        response = client.get(url if route == "list" else f"{url}/1")
    assert_error(response, 404, "task_not_found")


def test_missing_comment(task_url):
    assert_error(client.get(f"{task_url}/999999"), 404, "comment_not_found")


@pytest.mark.parametrize("body", ["", "   ", "\t\n\r", "\u2003"])
def test_blank_body_uses_validation_envelope(task_url, body):
    response = client.post(task_url, json={"body": body})
    assert_error(response, 422, "validation_error")
    assert response.json() == {
        "error": {
            "code": "validation_error", "message": "comment body must not be blank",
        }
    }
    assert client.get(task_url).json()["items"] == []


def test_list_pagination_walk_is_task_scoped(task_url, fresh_store):
    other = fresh_store.create("other fictional task", "todo")
    expected = []
    for i in range(5):
        fresh_store.create_comment(other["id"], "excluded")
        expected.append(client.post(task_url, json={"body": f"text {i}"}).json())
    cursor = 0
    seen = []
    for count in (2, 2, 1):
        response = client.get(task_url, params={"cursor": cursor, "limit": 2})
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == count
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if count == 2:
            assert cursor == page["items"][-1]["id"]
        else:
            assert cursor is None
    assert seen == expected
    assert client.get(task_url, params={"cursor": seen[-1]["id"]}).json() == {
        "items": [], "next_cursor": None,
    }


@pytest.mark.parametrize("limit", [0, -10])
def test_pagination_clamps_lower_bounds(task_url, limit):
    first = client.post(task_url, json={"body": "first"}).json()
    client.post(task_url, json={"body": "second"})
    response = client.get(task_url, params={"cursor": -5, "limit": limit})
    assert response.status_code == 200
    assert response.json() == {"items": [first], "next_cursor": first["id"]}


def test_pagination_default_and_upper_limit(task_url, fresh_store):
    for i in range(201):
        fresh_store.create_comment(1, f"text {i}")
    default = client.get(task_url).json()
    assert len(default["items"]) == 50
    assert default["next_cursor"] == default["items"][-1]["id"]
    upper = client.get(task_url, params={"limit": 999}).json()
    assert len(upper["items"]) == 200
    assert upper["next_cursor"] == upper["items"][-1]["id"]


@pytest.mark.parametrize("route", ["create", "list", "get"])
def test_down_store(route, tmp_path, monkeypatch):
    broken = main.TaskStore(str(tmp_path / "missing-dir" / "tasks.db"))
    monkeypatch.setattr(main, "store", broken)
    url = "/v1/tasks/1/comments"
    if route == "create":
        response = client.post(url, json={"body": "x"})
    else:
        response = client.get(url if route == "list" else f"{url}/1")
    assert_error(response, 503, "store_unavailable")
    assert response.json()["error"]["message"] == "task store is unavailable"


def test_health_degraded_until_comments_schema_is_migrated(tmp_path, monkeypatch):
    legacy = main.TaskStore(str(tmp_path / "legacy.db"))
    # Only the pre-TF-106 tasks schema, independent of the current migration.
    with sqlite3.connect(legacy.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks (status, id)"
        )
    monkeypatch.setattr(main, "store", legacy)
    task = legacy.create("fictional task before upgrade", "todo")

    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded", "checks": {"task_store": "down"},
    }

    main.migrate(legacy.db_path)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    response = client.post(
        f"/v1/tasks/{task['id']}/comments", json={"body": "after upgrade"}
    )
    assert response.status_code == 201
    assert response.json() == {
        "id": 1, "task_id": task["id"], "body": "after upgrade",
        "supersedes_id": None,
    }


def test_migration_preserves_comments_and_creates_paging_index(fresh_store):
    task = fresh_store.create("fictional task", "todo")
    comment = fresh_store.create_comment(task["id"], "preserved")
    main.migrate(fresh_store.db_path)
    main.migrate(fresh_store.db_path)
    assert main.TaskStore(fresh_store.db_path).get_comment(task["id"], comment["id"]) == comment
    with fresh_store._connect() as conn:
        columns = conn.execute("PRAGMA index_info(idx_comments_task_id_id)").fetchall()
    assert [row["name"] for row in columns] == ["task_id", "id"]


def test_simultaneous_revisions_cannot_fork(fresh_store):
    task = fresh_store.create("fictional task", "todo")
    original = fresh_store.create_comment(task["id"], "v1")
    barrier = Barrier(2)

    def revise(body):
        barrier.wait(timeout=5)
        try:
            fresh_store.create_comment(task["id"], body, original["id"])
        except main.CommentConflict:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, ("v2a", "v2b")))
    assert sorted(results) == ["conflict", "created"]
    items, cursor = fresh_store.list_comments_page(task["id"], 0, 50)
    assert len(items) == 2
    assert items[0] == original
    assert items[1]["supersedes_id"] == original["id"]
    assert cursor is None
