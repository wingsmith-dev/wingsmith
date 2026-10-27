"""Auth: the principal seam in `app.auth.manager`, its wiring into the HTTP
API (`app.core.context`, `app.auth.routes`) and the hub (`app.hub.server`).
"""

import json
import time
import uuid
import base64
import socket
import asyncio

import pytest

from litestar.testing import TestClient
from websockets.exceptions import InvalidStatus
from websockets.asyncio.client import connect

from app.db import engine as db_engine
from app.auth import LOGIN_GUESSES
from app.auth import LOCAL_GATEWAY_SERVICE_TOKEN
from app.auth import AuthManager
from app.auth import AuthService
from app.auth import service as auth_service
from app.config import settings
from app.api.app import create_app
from app.hub.server import serve_forever
from app.hub.server import _resolve_connection
from app.auth.routes import LOGIN_CEILING
from app.auth.routes import TENANT_GUESSES
from app.config.settings import Settings
from app.config.settings import load
from app.config.settings import api_host
from app.config.settings import validate
from app.config.settings import resolved_admin_auth
from app.core.exceptions import NotAuthorized


SESSION = "0f5a4c1e9b3d4e7f8a2b6c9d1e3f5a7b"
"""A session id for tokens that never reach a database."""

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _reset_auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "")
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "")
    monkeypatch.setattr(settings, "SESSION_TTL_SECONDS", 900)
    for budget in (LOGIN_GUESSES, LOGIN_CEILING, TENANT_GUESSES):
        budget.clear()
    yield
    for budget in (LOGIN_GUESSES, LOGIN_CEILING, TENANT_GUESSES):
        budget.clear()


def enable_admin_auth(monkeypatch, password: str = PASSWORD) -> None:
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", AuthManager.hash_admin_password(password))
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "test-signing-secret")


@pytest.fixture
def plane_db(database, monkeypatch):
    """Points the app's own engine at the migrated test database, never the dev one."""
    monkeypatch.setattr(settings, "DATABASE_URL", database)
    monkeypatch.setattr(db_engine, "_engine", None)
    monkeypatch.setattr(db_engine, "_factory", None)
    return database


def signed(claims: dict) -> str:
    """A session token with exactly these claims, correctly signed."""
    payload = json.dumps(claims).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"hgrs1.{encoded}.{AuthManager._sign(payload)}"


def claims(**overrides) -> dict:
    base = {"k": "admin", "n": "admin", "s": None, "sid": SESSION, "g": AuthManager.admin_generation()}
    return {**base, "exp": int(time.time()) + 900, **overrides}


# --- password hashing -------------------------------------------------------


def test_password_hash_round_trips():
    digest = AuthManager.hash_admin_password(PASSWORD)
    assert digest != PASSWORD
    assert AuthManager.verify_admin_password(PASSWORD, digest)


def test_wrong_password_does_not_verify():
    digest = AuthManager.hash_admin_password(PASSWORD)
    assert not AuthManager.verify_admin_password("wrong password", digest)


def test_empty_hash_never_verifies():
    assert not AuthManager.verify_admin_password(PASSWORD, "")


# --- session tokens ----------------------------------------------------------


def test_session_token_round_trips(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    principal = AuthManager.verify_session_token(token)
    assert principal is not None
    assert principal.kind == "admin"


def test_expired_session_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin", session_id=SESSION, ttl_seconds=-1)
    assert AuthManager.verify_session_token(token) is None


def test_tampered_session_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    prefix, payload, signature = token.split(".")
    tampered = f"{prefix}.{payload}.{signature[:-1]}{'a' if signature[-1] != 'a' else 'b'}"
    assert AuthManager.verify_session_token(tampered) is None


def test_tampered_session_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    prefix, payload, signature = token.split(".")
    forged = f"{prefix}.{payload}extra.{signature}"
    assert AuthManager.verify_session_token(forged) is None


def test_malformed_session_token_is_rejected():
    assert AuthManager.verify_session_token("not-a-session-token") is None
    assert AuthManager.verify_session_token("hgrs1.onlyonepart") is None


def test_session_token_signed_under_a_different_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-a")
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-b")
    assert AuthManager.verify_session_token(token) is None


def test_a_session_token_names_its_session(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    principal = AuthManager.verify_session_token(AuthManager.issue_session_token("admin", session_id=SESSION))
    assert principal is not None
    assert principal.session_id == SESSION


def test_no_session_is_minted_for_a_service_or_operator_kind():
    for kind in ("gateway", "operator", "root"):
        with pytest.raises(ValueError):
            AuthManager.issue_session_token(kind, kind=kind, session_id=SESSION)


@pytest.mark.parametrize("kind", ["gateway", "operator", "superuser", ""])
def test_a_signed_session_of_any_other_kind_is_refused(monkeypatch, kind):
    """Only `admin` and `tenant` are allowlisted; an unknown kind never falls through to admin."""
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    assert AuthManager.verify_session_token(signed(claims(k=kind))) is None


def test_a_signed_session_missing_or_adding_a_field_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    assert AuthManager.verify_session_token(signed(claims())) is not None
    without_session = claims()
    del without_session["sid"]
    assert AuthManager.verify_session_token(signed(without_session)) is None
    assert AuthManager.verify_session_token(signed(claims(scopes=["*"]))) is None


def test_the_two_field_payload_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    payload = f"admin:{int(time.time()) + 900}".encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    assert AuthManager.verify_session_token(f"hgrs1.{encoded}.{AuthManager._sign(payload)}") is None


def test_changing_the_admin_password_ends_every_admin_session(monkeypatch):
    enable_admin_auth(monkeypatch)
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    assert AuthManager.verify_session_token(token) is not None
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", AuthManager.hash_admin_password("a new password"))
    assert AuthManager.verify_session_token(token) is None


def test_a_refresh_token_carries_its_session_and_generation():
    token, digest = AuthManager.mint_refresh_token(SESSION, "gen123")
    assert token.startswith("hgrr1.")
    assert AuthManager.parse_refresh_token(token) == (SESSION, "gen123")
    assert digest == AuthManager.hash_refresh_token(token)
    assert AuthManager.parse_refresh_token("hgrr1.onlyrandom") is None
    assert AuthManager.parse_refresh_token(f"hgrs1.{SESSION}.gen.x") is None


# --- resolve_gateway / resolve_principal -------------------------------------


def test_resolve_gateway_accepts_the_local_default_in_local_env(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local")
    principal = AuthManager.resolve_gateway(LOCAL_GATEWAY_SERVICE_TOKEN)
    assert principal is not None
    assert principal.kind == "gateway"


def test_the_local_gateway_default_is_refused_outside_local(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert AuthManager.resolve_gateway(LOCAL_GATEWAY_SERVICE_TOKEN) is None


def test_an_unset_gateway_token_outside_local_matches_nothing(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert AuthManager.resolve_gateway("") is None
    assert AuthManager.resolve_gateway(None) is None


def test_resolve_gateway_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "the-real-token")
    assert AuthManager.resolve_gateway("some-other-token") is None


def test_resolve_gateway_accepts_the_configured_token(monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "the-real-token")
    principal = AuthManager.resolve_gateway("the-real-token")
    assert principal is not None
    assert principal.kind == "gateway"


def test_no_http_credential_resolves_to_an_operator(monkeypatch):
    """Operator credentials open hub sessions and nothing else."""
    monkeypatch.setattr(settings, "APP_ENV", "local")
    token, _ = AuthManager.mint_enrolment_token()
    for candidate in (token, LOCAL_GATEWAY_SERVICE_TOKEN, "hangar-local-operator-token", None):
        assert AuthManager.resolve_principal(candidate).kind != "operator"
    assert AuthManager.resolve_operator("hangar-local-operator-token") is None


def test_resolve_principal_with_admin_auth_disabled_accepts_no_token():
    principal = AuthManager.resolve_principal(None)
    assert principal.kind == "admin"


def test_resolve_principal_with_admin_auth_enabled_rejects_no_token(monkeypatch):
    enable_admin_auth(monkeypatch)
    with pytest.raises(NotAuthorized):
        AuthManager.resolve_principal(None)


def test_resolve_principal_with_admin_auth_enabled_rejects_wrong_token(monkeypatch):
    enable_admin_auth(monkeypatch)
    with pytest.raises(NotAuthorized):
        AuthManager.resolve_principal("garbage")


def test_resolve_principal_accepts_a_session_token_regardless_of_admin_auth_mode(monkeypatch):
    enable_admin_auth(monkeypatch)
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    principal = AuthManager.resolve_principal(token)
    assert principal.kind == "admin"


def test_resolve_principal_prefers_the_gateway_token_over_the_admin_bypass(monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "gw-token")
    principal = AuthManager.resolve_principal("gw-token")
    assert principal.kind == "gateway"


# --- admin auth mode resolution / settings.validate --------------------------


def test_admin_auth_defaults_disabled_in_local():
    s = Settings(APP_ENV="local")
    assert resolved_admin_auth(s) == "disabled"
    assert not AuthManager.admin_auth_enabled() or settings.APP_ENV != "local"


def test_admin_auth_defaults_enabled_outside_local():
    s = Settings(APP_ENV="production")
    assert resolved_admin_auth(s) == "enabled"


def test_admin_auth_explicit_flag_wins_over_the_default_in_local():
    assert resolved_admin_auth(Settings(APP_ENV="local", ADMIN_AUTH="enabled")) == "enabled"


def test_admin_auth_cannot_be_disabled_outside_local(monkeypatch):
    assert resolved_admin_auth(Settings(APP_ENV="production", ADMIN_AUTH="disabled")) == "enabled"
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "ADMIN_AUTH", "disabled")
    assert AuthManager.admin_auth_enabled()
    with pytest.raises(NotAuthorized):
        AuthManager.resolve_principal(None)


def test_validate_refuses_to_boot_admin_enabled_without_password_hash():
    s = Settings(APP_ENV="production", ADMIN_AUTH="enabled", SESSION_SIGNING_SECRET="sig")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_refuses_to_boot_admin_enabled_without_signing_secret():
    s = Settings(APP_ENV="production", ADMIN_AUTH="enabled", ADMIN_PASSWORD_HASH="hash")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_refuses_to_boot_with_admin_auth_disabled_outside_local():
    s = Settings(APP_ENV="production", ADMIN_AUTH="disabled", SESSION_SIGNING_SECRET="sig", ADMIN_PASSWORD_HASH="hash")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_refuses_to_boot_without_a_signing_secret_whatever_admin_auth_says():
    with pytest.raises(RuntimeError):
        validate(Settings(APP_ENV="production", ADMIN_PASSWORD_HASH="hash"))


def test_validate_passes_outside_local_with_both_secrets():
    validate(Settings(APP_ENV="production", SESSION_SIGNING_SECRET="sig", ADMIN_PASSWORD_HASH="hash"))


# --- fail-closed defaults ----------------------------------------------------


def test_no_session_is_signed_with_a_constant_outside_local(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    with pytest.raises(RuntimeError):
        AuthManager.signing_secret()


def test_the_local_signing_key_is_random_per_process(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local")
    secret = AuthManager.signing_secret()
    assert secret == AuthManager.signing_secret()
    assert len(secret) >= 32
    assert "hangar" not in secret


def test_the_api_binds_loopback_in_local_unless_told_otherwise():
    assert api_host(Settings(APP_ENV="local")) == "127.0.0.1"
    assert api_host(Settings(APP_ENV="local", API_HOST="0.0.0.0")) == "0.0.0.0"
    assert api_host(Settings(APP_ENV="production")) == "0.0.0.0"


def test_settings_read_only_prefixed_names():
    loaded = load({"DATABASE_URL": "postgresql+psycopg://elsewhere/db", "APP_ENV": "local"})
    assert loaded.DATABASE_URL == Settings().DATABASE_URL
    assert loaded.APP_ENV == "production"
    assert load({"HANGAR_APP_ENV": "local"}).APP_ENV == "local"


def test_an_unconfigured_plane_is_production_and_refuses_to_boot():
    """Started with no environment at all, the plane fails closed instead of opening as admin."""
    loaded = load({})
    assert loaded.APP_ENV == "production"
    assert resolved_admin_auth(loaded) == "enabled"
    with pytest.raises(RuntimeError, match="HANGAR_APP_ENV=local"):
        validate(loaded)


def test_a_bare_app_env_cannot_switch_auth_off():
    """A leftover `APP_ENV=local` in a shell or a container is ignored, so it cannot open the API."""
    loaded = load({"APP_ENV": "local", "ADMIN_AUTH": "disabled"})
    assert resolved_admin_auth(loaded) == "enabled"


def test_validate_is_frictionless_in_local_with_nothing_configured():
    validate(Settings(APP_ENV="local"))


def test_a_malformed_trusted_proxy_stops_the_boot():
    """Otherwise it is read on every sign-in, and every sign-in is a 500."""
    with pytest.raises(RuntimeError, match="'10.0.0.0/33'"):
        validate(Settings(APP_ENV="local", TRUSTED_PROXIES="10.0.0.0/8, 10.0.0.0/33"))
    with pytest.raises(RuntimeError, match="'ingress'"):
        validate(Settings(APP_ENV="local", TRUSTED_PROXIES="ingress"))
    validate(Settings(APP_ENV="local", TRUSTED_PROXIES=" 10.0.0.0/8 , fd00::/8, 192.0.2.1 ,"))


# --- HTTP API wiring -----------------------------------------------------


@pytest.mark.database
def test_unauthenticated_request_succeeds_when_admin_auth_is_disabled(plane_db):
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/clusters")
    assert response.status_code == 200


def test_health_never_requires_auth(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_no_token_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/clusters")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "not_authorized"
    assert "message" in body and "detail" in body


def test_malformed_authorization_header_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/clusters", headers={"Authorization": "not-a-bearer-header"})
    assert response.status_code == 401


def test_wrong_token_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/clusters", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_the_gateway_token_is_403_on_an_admin_route(monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "gw-token")
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/clusters", headers={"Authorization": "Bearer gw-token"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_an_enrolment_token_is_401_on_the_http_api(monkeypatch):
    """A cluster's credential opens its hub session; on the API it is no credential at all."""
    enable_admin_auth(monkeypatch)
    token, _ = AuthManager.mint_enrolment_token()
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/clusters/alpha/enrolment-token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.database
def test_admin_login_succeeds_with_the_right_password(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 201 or response.status_code == 200
    body = response.json()
    assert "access_token" in body and body["expires_in"] > 0
    assert "refresh_token" in body


def test_admin_login_fails_with_the_wrong_password(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/auth/login", json={"password": "not it"})
    assert response.status_code == 401
    assert response.json()["code"] == "not_authorized"


def test_repeated_wrong_passwords_are_throttled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for _ in range(LOGIN_GUESSES.limit):
            assert client.post("/v1/auth/login", json={"password": "not it"}).status_code == 401
        response = client.post("/v1/auth/login", json={"password": "not it"})
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


def test_throttled_login_refuses_the_right_password_too(monkeypatch):
    """The budget is checked before the hash, so a spent window refuses everyone.

    That is the tradeoff `LOGIN_GUESSES` documents: bounding the bcrypt an
    anonymous caller can buy costs that caller a minute of lockout.
    """
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for _ in range(LOGIN_GUESSES.limit):
            client.post("/v1/auth/login", json={"password": "not it"})
        response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 429


@pytest.mark.database
def test_a_correct_login_does_not_spend_the_budget(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for _ in range(LOGIN_GUESSES.limit * 2):
            assert client.post("/v1/auth/login", json={"password": PASSWORD}).status_code in (200, 201)
    assert not LOGIN_GUESSES.spent("testclient|admin")


def _from(monkeypatch, header: str = "X-Test-Client") -> None:
    """Charges failures to an address the test names, since the test client has one peer."""
    monkeypatch.setattr("app.auth.routes.client_address", lambda request: request.headers[header])


def test_one_caller_spending_the_admin_budget_does_not_lock_out_another(monkeypatch):
    enable_admin_auth(monkeypatch)
    _from(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        attacker = {"X-Test-Client": "203.0.113.7"}
        for _ in range(LOGIN_GUESSES.limit):
            client.post("/v1/auth/login", json={"password": "not it"}, headers=attacker)
        assert client.post("/v1/auth/login", json={"password": "not it"}, headers=attacker).status_code == 429
        operator = {"X-Test-Client": "198.51.100.20"}
        response = client.post("/v1/auth/login", json={"password": "still not it"}, headers=operator)
    assert response.status_code == 401


def test_the_admin_ceiling_bounds_bcrypt_across_many_callers(monkeypatch):
    enable_admin_auth(monkeypatch)
    _from(monkeypatch)
    monkeypatch.setattr(LOGIN_CEILING, "limit", 3)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for n in range(3):
            client.post("/v1/auth/login", json={"password": "x"}, headers={"X-Test-Client": f"192.0.2.{n}"})
        response = client.post("/v1/auth/login", json={"password": "x"}, headers={"X-Test-Client": "192.0.2.99"})
    assert response.status_code == 429


def test_tenant_failures_never_spend_the_admin_budget(monkeypatch):
    """Wrong tenant tokens are charged to the tenant they named, not to the operator."""
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(TENANT_GUESSES, "limit", 1)
    TENANT_GUESSES.fail("testclient|globex")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        tenant = client.post("/v1/auth/login", json={"slug": "globex", "password": "hgrt1.nope"})
        admin = client.post("/v1/auth/login", json={"password": "not it"})
    assert tenant.status_code == 429
    assert admin.status_code == 401


def test_the_schema_is_served_outside_local_with_the_package_version(monkeypatch):
    from importlib import metadata

    monkeypatch.setattr(settings, "APP_ENV", "production")
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/schema/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["info"]["version"] == metadata.version("hangar-plane")
    assert "/v1/clusters" in document["paths"]


@pytest.mark.database
def test_session_token_from_login_is_accepted_on_a_protected_route(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        login = client.post("/v1/auth/login", json={"password": PASSWORD})
        token = login.json()["access_token"]
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_expired_session_token_is_401_on_a_protected_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        token = AuthManager.issue_session_token("admin", session_id=SESSION, ttl_seconds=-1)
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_session_that_has_ended_is_401_on_the_next_request(monkeypatch):
    """Logout and revocation take effect at the guard, not when the access token expires."""
    enable_admin_auth(monkeypatch)

    async def ended(principal) -> bool:
        return False

    monkeypatch.setattr("app.core.context.session_is_live", ended)
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/system/config", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_live_session_reaches_the_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    seen = []

    async def live(principal) -> bool:
        seen.append(principal.session_id)
        return True

    monkeypatch.setattr("app.core.context.session_is_live", live)
    token = AuthManager.issue_session_token("admin", session_id=SESSION)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/v1/system/config", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert seen == [SESSION]


# --- sessions against the database ---------------------------------------


def login(client: TestClient) -> dict:
    response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code in (200, 201), response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.database
def test_refresh_rotates_the_pair_and_keeps_the_session(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        first = login(client)
        rotated = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert rotated.status_code in (200, 201)
        second = rotated.json()
        assert second["refresh_token"] != first["refresh_token"]
        assert client.get("/v1/system/config", headers=bearer(second["access_token"])).status_code == 200
        assert client.get("/v1/system/config", headers=bearer(first["access_token"])).status_code == 200


@pytest.mark.database
def test_reusing_a_rotated_refresh_token_ends_the_whole_session(plane_db, monkeypatch):
    """Two parties hold the session when a rotated token comes back, so neither keeps it."""
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(auth_service, "REFRESH_RACE_GRACE_SECONDS", 0.0)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        first = login(client)
        second = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
        replay = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert replay.status_code == 401
        assert client.post("/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401
        assert client.get("/v1/system/config", headers=bearer(second["access_token"])).status_code == 401


@pytest.mark.database
def test_logout_ends_the_session_and_its_access_tokens(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        pair = login(client)
        assert client.get("/v1/system/config", headers=bearer(pair["access_token"])).status_code == 200
        assert client.post("/v1/auth/logout", json={"refresh_token": pair["refresh_token"]}).status_code == 204
        assert client.get("/v1/system/config", headers=bearer(pair["access_token"])).status_code == 401
        assert client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 401
        assert client.post("/v1/auth/logout", json={"refresh_token": pair["refresh_token"]}).status_code == 204


@pytest.mark.database
def test_a_password_change_refuses_the_old_sessions_refresh(plane_db, monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        pair = login(client)
        monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", AuthManager.hash_admin_password("rotated"))
        assert client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code == 401
        assert client.get("/v1/system/config", headers=bearer(pair["access_token"])).status_code == 401


@pytest.mark.database
def test_rotation_never_extends_the_session(plane_db, monkeypatch):
    """`REFRESH_TTL_SECONDS` is an absolute lifetime: every rotated row keeps the first row's expiry."""
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        token = login(client)["refresh_token"]
        for _ in range(3):
            token = client.post("/v1/auth/refresh", json={"refresh_token": token}).json()["refresh_token"]

    session_id, _ = AuthManager.parse_refresh_token(token)

    async def expiries():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(plane_db)
        try:
            async with engine.connect() as connection:
                root = await connection.execute(
                    text("SELECT expires_at FROM auth_refresh_token WHERE id = :id"), {"id": uuid.UUID(session_id)}
                )
                latest = await connection.execute(
                    text("SELECT expires_at FROM auth_refresh_token WHERE token_hash = :hash"),
                    {"hash": AuthManager.hash_refresh_token(token)},
                )
                return root.scalar_one(), latest.scalar_one()
        finally:
            await engine.dispose()

    root, latest = asyncio.run(expiries())
    assert root == latest


@pytest.mark.database
async def test_two_racing_refreshes_rotate_exactly_once(sessions, monkeypatch):
    enable_admin_auth(monkeypatch)
    async with sessions() as db:
        token = await AuthService(db).create_refresh_token(name="admin")

    async def rotate():
        async with sessions() as db:
            return await AuthService(db).rotate_refresh_token(token)

    results = await asyncio.gather(rotate(), rotate())
    assert sum(result is not None for result in results) == 1

    winner = next(result for result in results if result is not None)
    refresh, access, _, _ = winner
    principal = AuthManager.verify_session_token(access)
    async with sessions() as db:
        assert await AuthService(db).session_is_live(principal), "losing the race must not end the session"
    async with sessions() as db:
        assert await AuthService(db).rotate_refresh_token(refresh) is not None


@pytest.mark.database
async def test_a_late_copy_of_a_just_rotated_token_is_refused_without_ending_the_session(sessions, monkeypatch):
    """A duplicated tab refreshing a moment after the original is a race, not a theft."""
    enable_admin_auth(monkeypatch)
    async with sessions() as db:
        token = await AuthService(db).create_refresh_token(name="admin")
    async with sessions() as db:
        rotated = await AuthService(db).rotate_refresh_token(token)
    async with sessions() as db:
        assert await AuthService(db).rotate_refresh_token(token) is None
    principal = AuthManager.verify_session_token(rotated[1])
    async with sessions() as db:
        assert await AuthService(db).session_is_live(principal)


@pytest.mark.database
async def test_a_copy_presented_after_the_grace_window_ends_the_session(sessions, monkeypatch):
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(auth_service, "REFRESH_RACE_GRACE_SECONDS", 0.0)
    async with sessions() as db:
        token = await AuthService(db).create_refresh_token(name="admin")
    async with sessions() as db:
        rotated = await AuthService(db).rotate_refresh_token(token)
    await asyncio.sleep(0.01)
    async with sessions() as db:
        assert await AuthService(db).rotate_refresh_token(token) is None
    principal = AuthManager.verify_session_token(rotated[1])
    async with sessions() as db:
        assert not await AuthService(db).session_is_live(principal)


@pytest.mark.database
async def test_a_refresh_token_in_the_old_format_is_refused(sessions, monkeypatch):
    enable_admin_auth(monkeypatch)
    async with sessions() as db:
        assert await AuthService(db).rotate_refresh_token("hgrr1.some-random-value") is None


def test_cors_allowed_origins_are_configurable(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.options(
            "/v1/clusters",
            headers={
                "Origin": "https://console.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") == "https://console.example.com"


def test_a_cors_preflight_needs_no_credential_with_admin_auth_enabled(monkeypatch):
    """A browser sends no Authorization on a preflight; refusing it would break the console."""
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.options(
            "/v1/clusters",
            headers={
                "Origin": "https://console.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert response.status_code == 204
    assert response.headers.get("access-control-allow-origin") == "https://console.example.com"


def test_cors_rejects_an_unconfigured_origin(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.options(
            "/v1/clusters",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"


# --- cross-site writes ---------------------------------------------------


def test_a_write_from_a_foreign_origin_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/projects", json={"name": "x"}, headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert response.json()["detail"] == {"origin": "https://evil.example.com"}


def test_a_null_origin_is_refused():
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/auth/login", json={"password": "x"}, headers={"Origin": "null"})
    assert response.status_code == 403


def test_a_write_from_an_allowed_or_the_serving_origin_passes_the_check(monkeypatch):
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for origin in ("https://console.example.com", f"http://{client.base_url.netloc.decode()}"):
            response = client.post("/v1/auth/login", json={"password": "not it"}, headers={"Origin": origin})
            assert response.status_code == 401, origin


def test_a_write_with_a_non_json_body_is_415():
    """A cross-site form can post `text/plain` without a preflight; nothing here reads it."""
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/auth/login", content=b'{"password": "x"}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_a_rebound_host_is_refused_while_admin_auth_is_off():
    """DNS rebinding: the attacker's page names itself in `Origin` and `Host` alike."""
    with TestClient(app=create_app(), base_url="http://evil.test:8000") as client:
        write = client.post("/v1/projects", json={"name": "x"}, headers={"Origin": "http://evil.test:8000"})
        read = client.get("/v1/clusters")
    assert write.status_code == 403
    assert write.json()["detail"] == {"host": "evil.test:8000"}
    assert read.status_code == 403


def test_the_hosts_the_plane_is_known_by_pass_while_admin_auth_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    monkeypatch.setattr(settings, "PUBLIC_HOST", "hangar.example.com")
    hosts = ["localhost:8000", "127.0.0.1", "[::1]:8000", "console.example.com", "HANGAR.example.com:443"]
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        for host in hosts:
            response = client.get("/health", headers={"Host": host})
            assert response.status_code == 200, host
        assert client.get("/health", headers={"Host": "localhost.evil.test"}).status_code == 403


def test_an_unknown_host_is_not_refused_while_admin_auth_is_on(monkeypatch):
    """A rebound page has no credential to borrow then, and a deployment need not list its names."""
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://hangar.internal:8000") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/clusters").status_code == 401


def test_a_bodiless_write_needs_no_content_type(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app(), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/v1/clusters/alpha/enrolment-token")
    assert response.status_code == 401


# --- enrolment tokens ----------------------------------------------------


def test_mint_enrolment_token_is_prefixed_and_hashes_to_its_digest():
    token, digest = AuthManager.mint_enrolment_token()
    assert token.startswith("hgro1.")
    assert digest == AuthManager.hash_enrolment_token(token)
    assert len(digest) == 64


def test_minted_enrolment_tokens_are_unique():
    a, ha = AuthManager.mint_enrolment_token()
    b, hb = AuthManager.mint_enrolment_token()
    assert a != b and ha != hb


def test_looks_like_enrolment_only_accepts_the_prefix():
    token, _ = AuthManager.mint_enrolment_token()
    assert AuthManager.looks_like_enrolment(token)
    assert not AuthManager.looks_like_enrolment(None)
    assert not AuthManager.looks_like_enrolment("")
    assert not AuthManager.looks_like_enrolment("some-fleet-token")


def test_bound_operator_principal_carries_the_cluster_in_scope():
    bound = AuthManager.operator_principal(cluster_uid="clu_123", cluster_name="ap")
    assert bound.kind == "operator"
    assert bound.scope == "clu_123"
    assert bound.name == "ap"


def test_there_is_no_unbound_operator_principal():
    with pytest.raises(ValueError):
        AuthManager.operator_principal(cluster_uid="", cluster_name="ap")


@pytest.mark.asyncio
async def test_resolve_connection_refuses_a_shared_fleet_token():
    """Only a per-cluster enrolment token opens a hub session."""
    assert await _resolve_connection("hangar-local-operator-token") is None
    assert await _resolve_connection(LOCAL_GATEWAY_SERVICE_TOKEN) is None


@pytest.mark.asyncio
async def test_resolve_connection_refuses_a_token_that_is_not_enrolment_shaped():
    assert await _resolve_connection("random-garbage") is None
    assert await _resolve_connection(None) is None


# --- hub -----------------------------------------------------------------


@pytest.fixture
def hub_port(monkeypatch) -> int:
    """A port nothing else holds, so a hub left over from another test or run cannot answer instead."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "HUB_PORT", port)
    return port


async def _until_listening(port: int, server_task: asyncio.Task, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if server_task.done():
            server_task.result()
            raise RuntimeError("the hub stopped before it accepted a connection")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)
            continue
        writer.close()
        await writer.wait_closed()
        return


async def _run_hub_and(port: int, coro):
    stop: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    server_task = asyncio.create_task(serve_forever(stop))
    await _until_listening(port, server_task)
    try:
        return await coro
    finally:
        stop.set_result(None)
        await server_task


@pytest.mark.asyncio
async def test_hub_rejects_a_missing_token(hub_port):

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{hub_port}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(hub_port, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_a_wrong_token(hub_port):

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(
                f"ws://127.0.0.1:{hub_port}{settings.HUB_PATH}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(hub_port, attempt())


@pytest.mark.asyncio
async def test_hub_refuses_a_shared_fleet_token_before_the_upgrade(hub_port):

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(
                f"ws://127.0.0.1:{hub_port}{settings.HUB_PATH}",
                additional_headers={"Authorization": "Bearer hangar-local-operator-token"},
            ):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(hub_port, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_bad_tokens_even_when_admin_auth_is_disabled(monkeypatch, hub_port):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "disabled")

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{hub_port}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(hub_port, attempt())


# --- the hash command ----------------------------------------------------


def test_the_hash_command_prints_a_hash_that_verifies(monkeypatch, capsys):
    from io import StringIO

    from app.auth.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(f"{PASSWORD}\n"))
    assert main(["hash", "--stdin"]) == 0
    digest = capsys.readouterr().out.strip()
    assert AuthManager.verify_admin_password(PASSWORD, digest)


def test_the_hash_command_refuses_a_short_password(monkeypatch, capsys):
    from io import StringIO

    from app.auth.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO("short\n"))
    assert main(["hash", "--stdin"]) == 1
    assert capsys.readouterr().out == ""
