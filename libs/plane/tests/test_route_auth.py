"""Every route on the API, walked: who is refused, and with which status.

The walk is over `create_app().routes`, so a route added tomorrow is covered the
day it lands. `401` means "no credential this API knows"; `403` means "a
credential, but not one this route is for". Nothing here needs a database: the
guard decides both before a handler or a session is reached.
"""

import re

import pytest

from litestar import get
from litestar.testing import TestClient

from app.auth import AuthManager
from app.config import settings
from app.api.app import create_app
from app.core.context import SERVICE_ONLY
from app.core.context import PUBLIC_ROUTES
from app.core.context import GATEWAY_ROUTES
from app.core.context import TENANT_SURFACE
from app.core.context import is_public


SESSION = "0f5a4c1e9b3d4e7f8a2b6c9d1e3f5a7b"
"""A session id for tokens that never reach a database."""


GATEWAY_TOKEN = "route-walk-gateway-token"


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "route-walk-signing-secret")
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", GATEWAY_TOKEN)


def _template(path: str) -> str:
    return re.sub(r"\{(\w+):[^}]*\}", r"{\1}", path)


def _concrete(path: str) -> str:
    return re.sub(r"\{\w+:[^}]*\}", "x", path)


def _routes() -> list[tuple[str, str]]:
    """`(method, path)` for every handler the app registers, Litestar's own excluded."""
    found = set()
    for route in create_app().routes:
        for method, entry in getattr(route, "route_handler_map", {}).items():
            handler = entry[0] if isinstance(entry, tuple) else entry
            if getattr(handler.fn, "__module__", "").startswith("litestar."):
                continue
            found.add((method, route.path))
    return sorted(found)


PROTECTED = [(method, path) for method, path in _routes() if not is_public(_template(path))]


def _session(kind: str) -> str:
    if kind == "tenant":
        return AuthManager.issue_session_token(name="globex", kind="tenant", scope="ten_01m1yd", session_id=SESSION)
    return AuthManager.issue_session_token(name="admin", session_id=SESSION)


@pytest.fixture(scope="module")
def client():
    """One app for the whole walk; settings are read per request, so the fixtures above still apply."""
    with TestClient(app=create_app()) as http:
        yield http


def _call(client: TestClient, method: str, path: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.request(method, _concrete(path), headers=headers)


def test_the_walk_is_not_vacuous():
    assert len(PROTECTED) > 100


def test_every_allowlisted_gateway_route_exists():
    """A stale entry would silently grant the gateway a route somebody adds later."""
    routes = {(method, _template(path)) for method, path in _routes()}
    assert routes >= GATEWAY_ROUTES


def test_the_public_routes_are_the_auth_routes_only():
    assert {_template(path) for _, path in _routes() if _template(path).startswith("/v1/")} >= PUBLIC_ROUTES
    assert all(route.startswith("/v1/auth/") for route in PUBLIC_ROUTES)


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_route_refuses_an_anonymous_caller(client, method, path):
    response = _call(client, method, path, None)
    assert response.status_code == 401, (method, path, response.text)
    assert response.json()["code"] == "not_authorized"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_route_refuses_an_unknown_bearer(client, method, path):
    response = _call(client, method, path, "hgrs1.not.real")
    assert response.status_code == 401, (method, path)


@pytest.mark.parametrize(("method", "path"), [(m, p) for m, p in PROTECTED if (m, _template(p)) not in GATEWAY_ROUTES])
def test_the_gateway_is_refused_everywhere_but_its_three_routes(client, method, path):
    response = _call(client, method, path, GATEWAY_TOKEN)
    assert response.status_code == 403, (method, path)
    assert response.json()["code"] == "forbidden"


def _tenant_may(method: str, path: str) -> bool:
    template = _template(path)
    return method == "GET" and (template == TENANT_SURFACE or template.startswith(f"{TENANT_SURFACE}/"))


@pytest.mark.parametrize(("method", "path"), [(m, p) for m, p in PROTECTED if not _tenant_may(m, p)])
def test_a_tenant_session_is_refused_outside_its_serving_view(client, method, path):
    response = _call(client, method, path, _session("tenant"))
    assert response.status_code == 403, (method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [(m, p) for m, p in PROTECTED if any(_template(p).startswith(prefix) for prefix in SERVICE_ONLY)],
)
def test_an_admin_session_is_refused_on_the_gateway_routes(client, method, path):
    """`/v1/gateway/tenants` carries key hashes, which never reach a console."""
    response = _call(client, method, path, _session("admin"))
    assert response.status_code == 403, (method, path)


def test_an_enrolment_token_is_refused_on_every_route(client):
    """An operator's credential is for the hub. On the API it names nobody."""
    token, _ = AuthManager.mint_enrolment_token()
    statuses = {_call(client, method, path, token).status_code for method, path in PROTECTED}
    assert statuses == {401}


def test_a_handler_that_never_asks_for_context_is_still_authenticated():
    """Authorization is not opt-in: leaving `context` out of a signature makes nothing public."""

    @get("/v1/probe-without-context", sync_to_thread=False)
    def probe() -> dict[str, str]:
        return {"reached": "yes"}

    app = create_app()
    app.register(probe)
    with TestClient(app=app) as client:
        response = client.get("/v1/probe-without-context")
    assert response.status_code == 401


def test_health_and_login_stay_public():
    with TestClient(app=create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/auth/login", json={"password": "nope"}).status_code == 401
