"""Validation-envelope tests (TF-105): malformed request input must
answer in the contract error envelope from taskflow-api-docs
("Errors"), never in FastAPI's default "detail" shape."""

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


def assert_contract_envelope(response, code="validation_error"):
    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_non_integer_limit_uses_contract_envelope():
    assert_contract_envelope(client.get("/v1/tasks?limit=abc"))


def test_non_integer_cursor_uses_contract_envelope():
    assert_contract_envelope(client.get("/v1/tasks?cursor=xyz"))


def test_missing_title_uses_contract_envelope():
    assert_contract_envelope(client.post("/v1/tasks", json={}))


def test_malformed_json_body_uses_contract_envelope():
    response = client.post(
        "/v1/tasks", content="{not json",
        headers={"Content-Type": "application/json"})
    assert_contract_envelope(response)


def test_validation_message_does_not_echo_submitted_value():
    response = client.get("/v1/tasks?limit=sekrit-value")
    assert_contract_envelope(response)
    assert "sekrit-value" not in response.json()["error"]["message"]
