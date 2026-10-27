"""Every operation in the published OpenAPI document, walked: what it answers.

`test_route_auth.py` covers who is refused. This covers what a caller who gets
through is told, driven by the document a client generates against, not
by the router, so the two cannot drift apart unnoticed.

The offline walk points the app at a database that is not there. Anything a
handler did would be a `500`, so every `400` below is decided before a handler
runs. The `database` walk then calls every collection against the migrated test
database and holds each answer to the document.
"""

import re

import pytest

from litestar.testing import TestClient

from app.db import engine as db_engine
from app.config import settings
from app.api.app import create_app
from app.core.context import GATEWAY_ROUTES


GATEWAY_TOKEN = "route-contract-gateway-token"

UNREACHABLE_DATABASE = "postgresql+psycopg://hangar:hangar@127.0.0.1:1/hangar"
"""Port 1: nothing listens there, so a handler that reaches for a session fails loudly."""

METHODS = ("get", "post", "put", "patch", "delete")


def _document() -> dict:
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        return client.get("/schema/openapi.json").json()


DOCUMENT = _document()

OPERATIONS = sorted(
    (method.upper(), path, operation)
    for path, item in DOCUMENT["paths"].items()
    for method, operation in item.items()
    if method in METHODS
)


def _id(value) -> str | None:
    return value if isinstance(value, str) else None


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _template(path: str) -> str:
    return re.sub(r"\{(\w+):[^}]*\}", r"{\1}", path)


def _headers(method: str, path: str) -> dict[str, str]:
    """The gateway's credential on its own routes, where an admin is refused; none elsewhere,
    which is the admin while admin auth is off."""
    if (method, path) in GATEWAY_ROUTES:
        return {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
    return {}


def _is_envelope(body: object) -> bool:
    return isinstance(body, dict) and {"code", "message", "detail"} <= body.keys()


@pytest.fixture(scope="module")
def offline():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "ADMIN_AUTH", "")
        patch.setattr(settings, "GATEWAY_SERVICE_TOKEN", GATEWAY_TOKEN)
        patch.setattr(settings, "DATABASE_URL", UNREACHABLE_DATABASE)
        patch.setattr(db_engine, "_engine", None)
        patch.setattr(db_engine, "_factory", None)
        with TestClient(app=create_app(), base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
            yield client


def test_the_walk_is_not_vacuous():
    assert len(OPERATIONS) > 150


def test_the_document_lists_every_registered_route_and_nothing_else():
    registered = set()
    for route in create_app().routes:
        for method, entry in getattr(route, "route_handler_map", {}).items():
            handler = entry[0] if isinstance(entry, tuple) else entry
            if method in ("HEAD", "OPTIONS") or getattr(handler.fn, "__module__", "").startswith("litestar."):
                continue
            registered.add((method, _template(route.path)))
    documented = {(method, path) for method, path, _ in OPERATIONS}
    assert documented == {route for route in registered if route[1].startswith(("/v1/", "/health"))}


@pytest.mark.parametrize(("method", "path", "operation"), OPERATIONS, ids=_id)
def test_every_operation_documents_its_success(method, path, operation):
    responses = set(operation.get("responses", {}))
    assert sum(code.startswith("2") for code in responses) == 1, (method, path, responses)


@pytest.mark.parametrize(
    ("method", "path", "operation"), [op for op in OPERATIONS if op[1].startswith("/v1/")], ids=_id
)
def test_every_operation_refuses_an_unknown_query_parameter(offline, method, path, operation):
    body = b"{}" if "requestBody" in operation else None
    response = offline.request(
        method,
        _concrete(path),
        params={"no_such_parameter": "1"},
        content=body,
        headers={"Content-Type": "application/json", **_headers(method, path)},
    )
    assert response.status_code == 400, (method, path, response.text)
    assert response.json()["code"] == "unknown_parameter"


@pytest.mark.parametrize(("method", "path", "operation"), [op for op in OPERATIONS if "requestBody" in op[2]], ids=_id)
def test_every_body_of_the_wrong_shape_is_refused_before_the_handler(offline, method, path, operation):
    response = offline.request(
        method,
        _concrete(path),
        content=b"[]",
        headers={"Content-Type": "application/json", **_headers(method, path)},
    )
    assert response.status_code == 400, (method, path, response.text)
    assert _is_envelope(response.json()), response.text


COLLECTIONS = [
    (method, path, operation)
    for method, path, operation in OPERATIONS
    if method == "GET" and path.startswith("/v1/") and "{" not in path
]


@pytest.fixture
def plane_db(database, monkeypatch):
    """Points the app's own engine at the migrated test database, never the dev one."""
    monkeypatch.setattr(settings, "ADMIN_AUTH", "")
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", GATEWAY_TOKEN)
    monkeypatch.setattr(settings, "DATABASE_URL", database)
    monkeypatch.setattr(settings, "METRICS_SOURCES", "")
    monkeypatch.setattr(db_engine, "_engine", None)
    monkeypatch.setattr(db_engine, "_factory", None)
    return database


@pytest.mark.database
@pytest.mark.parametrize(("method", "path", "operation"), COLLECTIONS, ids=_id)
def test_every_collection_answers_as_documented_against_the_database(plane_db, method, path, operation):
    """No 5xx from an empty, migrated plane with no metrics store: a success the document
    lists, or a refusal in the error envelope (a required query parameter, a route only a
    tenant reads). The one 5xx allowed is `metrics_unavailable`, which says the store is
    missing instead of that the plane failed."""
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
        response = client.get(path, headers=_headers(method, path))
    if response.status_code == 503:
        assert response.json()["code"] == "metrics_unavailable", (path, response.text)
        return
    assert response.status_code < 500, (path, response.text)
    if response.status_code < 300:
        assert str(response.status_code) in operation["responses"], path
        response.json()
    else:
        assert _is_envelope(response.json()), (path, response.text)
