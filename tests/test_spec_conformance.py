"""TF-107: exercise the curated taskflow-api-docs/openapi.yaml contract.

Behavior and response schemas are binding; curated descriptions need not
match FastAPI's generated documentation.
"""

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
SUCCESS_IDS = (
    "get_health", "list_tasks", "create_task", "get_task",
    "list_comments", "create_comment", "get_comment",
)


@pytest.fixture(scope="module")
def spec():
    root = Path(__file__).resolve().parents[1]
    path = Path(os.environ.get("TASKFLOW_SPEC_PATH", "../taskflow-api-docs/openapi.yaml"))
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        pytest.skip(f"TaskFlow OpenAPI spec absent at {path}; set TASKFLOW_SPEC_PATH")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    store = main.TaskStore(str(tmp_path / "tasks.db"))
    # taskflow-docs/deployment.md: migrate before serving requests.
    main.migrate(store.db_path)
    monkeypatch.setattr(main, "store", store)
    return store


def operations(document):
    return {
        (path, method): operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def operation_by_id(spec, operation_id):
    matches = [
        (path, method, operation)
        for (path, method), operation in operations(spec).items()
        if operation["operationId"] == operation_id
    ]
    assert len(matches) == 1, f"Expected exactly one operationId {operation_id}"
    return matches[0]


def validate(value, schema, spec, location="$Root"):
    """Validate the response-schema subset used by the curated contract."""
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/components/"), f"Unsupported reference: {ref}"
        target = spec
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate(value, target, spec, location)
        return
    if value is None and schema.get("nullable", False):
        return
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int}
    assert kind in types, f"{location}: unsupported schema type {kind}"
    # JSON booleans are not integers, although bool subclasses int in Python.
    assert type(value) is types[kind], f"{location}: expected {kind}, got {value!r}"
    if "enum" in schema:
        assert value in schema["enum"], f"{location}: {value!r} outside enum"
    if kind == "object":
        missing = set(schema.get("required", [])) - value.keys()
        assert not missing, f"{location}: missing required properties {sorted(missing)}"
        for name, child in schema.get("properties", {}).items():
            if name in value:
                validate(value[name], child, spec, f"{location}.{name}")
    elif kind == "array":
        for index, item in enumerate(value):
            validate(item, schema["items"], spec, f"{location}[{index}]")


def test_route_parity(spec):
    published = set(operations(spec))
    implemented = set(operations(main.api.openapi()))
    assert not published - implemented, f"Unimplemented spec routes: {published - implemented}"
    assert not implemented - published, f"Undocumented app routes: {implemented - published}"


def test_success_scenario_coverage(spec):
    ids = [operation["operationId"] for operation in operations(spec).values()]
    assert len(ids) == len(set(ids)), "Spec operationIds must be unique"
    assert set(ids) == set(SUCCESS_IDS), "Every spec operation needs a success scenario"


@pytest.fixture
def scenario_data(fresh_store):
    task = fresh_store.create("fictional conformance task", "todo")
    other = fresh_store.create("other fictional task", "doing")
    original = fresh_store.create_comment(task["id"], "original")
    revision = fresh_store.create_comment(task["id"], "revision", original["id"])
    foreign = fresh_store.create_comment(other["id"], "other task comment")
    return {
        "task_id": task["id"], "comment_id": original["id"],
        "revision_id": revision["id"], "foreign_id": foreign["id"],
    }


@pytest.mark.parametrize("operation_id", SUCCESS_IDS)
def test_success_shapes(spec, scenario_data, operation_id):
    path, method, operation = operation_by_id(spec, operation_id)
    scenarios = {
        "get_health": {},
        "list_tasks": {"params": {"limit": 1}},
        "create_task": {"json": {"title": "new fictional task", "status": "done"}},
        "get_task": {},
        "list_comments": {"params": {"limit": 1}},
        "create_comment": {
            "json": {"body": "next revision", "supersedes_id": scenario_data["revision_id"]},
        },
        "get_comment": {},
    }
    response = client.request(method, path.format(**scenario_data), **scenarios[operation_id])
    success_codes = {code for code in operation["responses"] if str(code).startswith("2")}
    assert str(response.status_code) in success_codes
    schema = operation["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
    validate(response.json(), schema, spec)


# taskflow-api-docs/errors.md and api-conventions.md ("Errors"):
# codes are stable; human-facing messages may change. Comment 404s hide
# cross-task existence; stale revisions conflict rather than fork a chain.
ERROR_SCENARIOS = [
    ("get_task", "missing_task", 404, "task_not_found"),
    ("list_comments", "missing_task", 404, "task_not_found"),
    ("create_comment", "missing_task", 404, "task_not_found"),
    ("get_comment", "missing_task", 404, "task_not_found"),
    ("get_comment", "missing_comment", 404, "comment_not_found"),
    ("create_comment", "missing_comment", 404, "comment_not_found"),
    ("get_comment", "cross_task", 404, "comment_not_found"),
    ("create_comment", "cross_task", 404, "comment_not_found"),
    ("create_comment", "stale_revision", 409, "conflict"),
    ("list_tasks", "invalid_status", 422, "invalid_status"),
    ("create_task", "invalid_status", 422, "invalid_status"),
    ("create_comment", "blank_body", 422, "validation_error"),
]


@pytest.mark.parametrize(
    "operation_id,scenario,status,code", ERROR_SCENARIOS,
    ids=[f"{op}-{scenario}" for op, scenario, _, _ in ERROR_SCENARIOS],
)
def test_documented_errors(spec, scenario_data, operation_id, scenario, status, code):
    path, method, operation = operation_by_id(spec, operation_id)
    data = dict(scenario_data)
    kwargs = {}
    if operation_id == "create_comment":
        kwargs["json"] = {"body": "fictional comment"}
    if scenario == "missing_task":
        data["task_id"] = 999999
    elif scenario in {"missing_comment", "cross_task", "stale_revision"}:
        comment_id = {
            "missing_comment": 999999,
            "cross_task": data["foreign_id"],
            "stale_revision": data["comment_id"],
        }[scenario]
        data["comment_id"] = comment_id
        if method == "post":
            kwargs["json"]["supersedes_id"] = comment_id
    elif scenario == "invalid_status":
        if method == "get":
            kwargs["params"] = {"status": "bogus"}
        else:
            kwargs["json"] = {"title": "fictional task", "status": "bogus"}
    elif scenario == "blank_body":
        kwargs["json"] = {"body": " \t\n "}
    response = client.request(method, path.format(**data), **kwargs)
    assert response.status_code == status
    assert str(status) in operation["responses"], f"Undeclared status for {operation_id}"
    body = response.json()
    declared = operation["responses"][str(status)]["content"]["application/json"]["schema"]
    validate(body, declared, spec)
    validate(body, {"$ref": "#/components/schemas/Error"}, spec)
    assert body["error"]["code"] == code
