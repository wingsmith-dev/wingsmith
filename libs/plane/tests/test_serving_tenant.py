"""The tenant serving view, and the boundary that is the whole reason it exists.

A tenant credential is the first identity this plane has issued that is not
trusted with the fleet, so the tests that matter here are the negative ones: what
a tenant principal cannot reach, and what it cannot see inside what it can.
"""

from types import SimpleNamespace

import pytest

from litestar.testing import TestClient

from app.auth import AuthManager
from app.config import settings
from app.api.app import create_app
from app.core.context import TENANT_SURFACE
from app.core.context import authorize
from app.core.exceptions import Forbidden
from app.modules.serving.services import ServingService


def _tenant_may(path: str, method: str = "GET") -> bool:
    try:
        authorize(AuthManager.tenant_principal(tenant_uid="ten_01m1yd", slug="globex"), method, path)
    except Forbidden:
        return False
    return True


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "test-signing-secret")


def tenant_session(uid: str = "ten_01m1yd", slug: str = "globex") -> str:
    return AuthManager.issue_session_token(name=slug, kind="tenant", scope=uid)


# --- the boundary -----------------------------------------------------------


def test_a_tenant_may_read_its_own_surface():
    assert _tenant_may(f"{TENANT_SURFACE}")
    assert _tenant_may(f"{TENANT_SURFACE}/models/moonshotai/kimi-k3")


@pytest.mark.parametrize(
    "path",
    ["/v1/clusters", "/v1/nodes", "/v1/endpoints", "/v1/endpoints/resolved", "/v1/tenants", "/v1/metrics/fleet"],
)
def test_a_tenant_may_not_read_anything_else(path):
    assert not _tenant_may(path)


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_a_tenant_may_not_write_even_on_its_own_surface(method):
    """Read-only is enforced by the method, so a write added later is refused by default."""
    assert not _tenant_may(TENANT_SURFACE, method=method)


def test_a_path_that_merely_starts_with_the_surface_word_is_not_the_surface():
    assert not _tenant_may("/v1/serving-quotas")


def test_a_tenant_session_is_403_on_a_fleet_route():
    """Authenticated but out of scope is `403`; `401` would tell the console to sign in again."""
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {tenant_session()}"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_tenant_session_is_403_on_the_routing_table():
    """The resolved table names every cluster we run. It is the one read that would hurt most."""
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/endpoints/resolved", headers={"Authorization": f"Bearer {tenant_session()}"})
    assert response.status_code == 403


# --- the session token ------------------------------------------------------


def test_a_tenant_session_resolves_to_a_tenant_principal():
    principal = AuthManager.verify_session_token(tenant_session())
    assert principal is not None
    assert principal.kind == "tenant"
    assert principal.scope == "ten_01m1yd"


def test_a_tenant_session_carrying_no_scope_is_refused():
    """A tenant principal with no tenant would be scoped to nothing, or to everything."""
    token = AuthManager.issue_session_token(name="globex", kind="tenant", scope=None)
    assert AuthManager.verify_session_token(token) is None


def test_a_session_minted_before_kinds_existed_still_reads_as_admin(monkeypatch):
    """Shipping this must not sign every operator out."""
    import time
    import base64

    payload = f"admin:{int(time.time()) + 900}".encode()
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    legacy = f"hgrs1.{payload_b64}.{AuthManager._sign(payload)}"
    principal = AuthManager.verify_session_token(legacy)
    assert principal is not None
    assert principal.kind == "admin"


def test_a_tenant_cannot_forge_its_scope_by_editing_the_payload():
    import base64

    token = tenant_session()
    prefix, payload_b64, signature = token.split(".")
    payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode()
    forged = payload.replace("ten_01m1yd", "ten_someoneelse").encode()
    forged_b64 = base64.urlsafe_b64encode(forged).rstrip(b"=").decode()
    assert AuthManager.verify_session_token(f"{prefix}.{forged_b64}.{signature}") is None


# --- the token itself -------------------------------------------------------


def test_a_tenant_token_is_recognisable_without_a_lookup():
    plaintext, _ = AuthManager.mint_tenant_token()
    assert AuthManager.looks_like_tenant(plaintext)
    assert not AuthManager.looks_like_tenant("hgro1.something")
    assert not AuthManager.looks_like_enrolment(plaintext)


def test_a_tenant_token_is_stored_only_as_a_digest():
    plaintext, digest = AuthManager.mint_tenant_token()
    assert plaintext not in digest
    assert digest == AuthManager.hash_tenant_token(plaintext)
    assert AuthManager.mint_tenant_token()[0] != plaintext


# --- what the view reports --------------------------------------------------


def endpoint(enabled: bool = True):
    return SimpleNamespace(enabled=enabled)


def test_a_model_with_a_ready_replica_is_serving():
    assert ServingService._state(endpoint(), 1) == "serving"


def test_a_model_with_no_ready_replica_is_degraded():
    assert ServingService._state(endpoint(), 0) == "degraded"


def test_a_model_somebody_turned_off_is_paused_and_not_degraded():
    """A decision we made must not read as an incident."""
    assert ServingService._state(endpoint(enabled=False), 0) == "paused"
    assert ServingService._state(endpoint(enabled=False), 3) == "paused"


def test_the_service_takes_its_tenant_at_construction():
    """A tenant that arrives per call is one somebody can forget to pass."""
    import inspect

    for name in ("overview", "model", "metrics_ref"):
        parameters = set(inspect.signature(getattr(ServingService, name)).parameters)
        assert "tenant_uid" not in parameters
        assert "tenant" not in parameters


def test_the_service_has_no_write_method():
    """An observed-and-borrowed surface has no writer here. Absent methods, not refusing ones."""
    banned = {"create", "update", "patch", "delete", "set", "add", "remove"}
    assert not banned & {name for name in dir(ServingService) if not name.startswith("_")}


def test_the_admin_view_of_a_customer_is_outside_the_tenant_surface():
    """The whole security argument for the path shape, stated as a test.

    Whose view an admin is reading is a path segment, so "may this caller name
    that tenant" is answered by the routing table: `/v1/tenants` is not
    `TENANT_SURFACE`, so a tenant credential never reaches the handler. The
    query-parameter shape this replaced asked the same question at request time,
    on the one surface a tenant *can* reach.
    """
    assert not _tenant_may("/v1/tenants/globex/serving")
    assert not _tenant_may("/v1/tenants/globex/serving/models/moonshotai/kimi-k3")


def test_a_tenant_session_is_403_on_another_tenants_serving_view():
    with TestClient(app=create_app()) as client:
        response = client.get(
            "/v1/tenants/someone-else/serving",
            headers={"Authorization": f"Bearer {tenant_session()}"},
        )
    assert response.status_code == 403


def test_the_tenant_scoped_surface_takes_no_tenant_from_the_caller():
    """The defect this shape was written for.

    `depends` used to read `?tenant=` for an admin, and no handler declared the
    parameter — so the request was refused as an unknown query parameter before
    the dependency that wanted it ever ran, and the admin view was unreachable
    by construction. Nothing on this surface reads a tenant from the request
    now, so there is no parameter to declare and none to forget.
    """
    from app.modules.serving.routes import ServingRoutes

    for handler in (ServingRoutes.overview, ServingRoutes.model, ServingRoutes.model_metrics):
        assert "tenant" not in handler.__annotations__
