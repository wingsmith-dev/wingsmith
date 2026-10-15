"""Auth: the principal seam in `app.auth.manager`, its wiring into the HTTP
API (`app.core.context`, `app.auth.routes`) and the hub (`app.hub.server`).
"""

import asyncio

import pytest

from litestar.testing import TestClient
from websockets.exceptions import InvalidStatus
from websockets.asyncio.client import connect

from app.auth import LOGIN_GUESSES
from app.auth import LOCAL_GATEWAY_SERVICE_TOKEN
from app.auth import AuthManager
from app.config import settings
from app.api.app import create_app
from app.hub.server import serve_forever
from app.hub.server import _resolve_connection
from app.config.settings import Settings
from app.config.settings import load
from app.config.settings import api_host
from app.config.settings import validate
from app.config.settings import resolved_admin_auth
from app.core.exceptions import NotAuthorized


PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _reset_auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "")
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "")
    monkeypatch.setattr(settings, "SESSION_TTL_SECONDS", 900)
    LOGIN_GUESSES.clear()
    yield
    LOGIN_GUESSES.clear()


def enable_admin_auth(monkeypatch, password: str = PASSWORD) -> None:
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", AuthManager.hash_admin_password(password))
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "test-signing-secret")


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
    token = AuthManager.issue_session_token("admin")
    principal = AuthManager.verify_session_token(token)
    assert principal is not None
    assert principal.kind == "admin"


def test_expired_session_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin", ttl_seconds=-1)
    assert AuthManager.verify_session_token(token) is None


def test_tampered_session_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin")
    prefix, payload, signature = token.split(".")
    tampered = f"{prefix}.{payload}.{signature[:-1]}{'a' if signature[-1] != 'a' else 'b'}"
    assert AuthManager.verify_session_token(tampered) is None


def test_tampered_session_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = AuthManager.issue_session_token("admin")
    prefix, payload, signature = token.split(".")
    forged = f"{prefix}.{payload}extra.{signature}"
    assert AuthManager.verify_session_token(forged) is None


def test_malformed_session_token_is_rejected():
    assert AuthManager.verify_session_token("not-a-session-token") is None
    assert AuthManager.verify_session_token("hgrs1.onlyonepart") is None


def test_session_token_signed_under_a_different_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-a")
    token = AuthManager.issue_session_token("admin")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-b")
    assert AuthManager.verify_session_token(token) is None


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
    token = AuthManager.issue_session_token("admin")
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
    loaded = load({"DATABASE_URL": "postgresql+psycopg://elsewhere/db", "APP_ENV": "production"})
    assert loaded.DATABASE_URL == Settings().DATABASE_URL
    assert loaded.APP_ENV == "local"
    assert load({"HANGAR_APP_ENV": "production"}).APP_ENV == "production"


def test_validate_is_frictionless_in_local_with_nothing_configured():
    validate(Settings(APP_ENV="local"))


# --- HTTP API wiring -----------------------------------------------------


@pytest.mark.database
def test_unauthenticated_request_succeeds_when_admin_auth_is_disabled():
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters")
    assert response.status_code == 200


def test_health_never_requires_auth(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_no_token_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "not_authorized"
    assert "message" in body and "detail" in body


def test_malformed_authorization_header_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters", headers={"Authorization": "not-a-bearer-header"})
    assert response.status_code == 401


def test_wrong_token_is_401_when_admin_auth_is_enabled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_the_gateway_token_is_403_on_an_admin_route(monkeypatch):
    monkeypatch.setattr(settings, "GATEWAY_SERVICE_TOKEN", "gw-token")
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters", headers={"Authorization": "Bearer gw-token"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_an_enrolment_token_is_401_on_the_http_api(monkeypatch):
    """A cluster's credential opens its hub session; on the API it is no credential at all."""
    enable_admin_auth(monkeypatch)
    token, _ = AuthManager.mint_enrolment_token()
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/clusters/alpha/enrolment-token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.database
def test_admin_login_succeeds_with_the_right_password(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 201 or response.status_code == 200
    body = response.json()
    assert "access_token" in body and body["expires_in"] > 0
    assert "refresh_token" in body


def test_admin_login_fails_with_the_wrong_password(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", json={"password": "not it"})
    assert response.status_code == 401
    assert response.json()["code"] == "not_authorized"


def test_repeated_wrong_passwords_are_throttled(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        for _ in range(LOGIN_GUESSES.limit):
            assert client.post("/v1/auth/login", json={"password": "not it"}).status_code == 401
        response = client.post("/v1/auth/login", json={"password": "not it"})
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


def test_throttled_login_refuses_the_right_password_too(monkeypatch):
    """The budget is checked before the hash, so a spent window refuses everyone.

    That is the tradeoff `LOGIN_GUESSES` documents: bounding the bcrypt an
    anonymous caller can buy costs the operator a minute of lockout.
    """
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        for _ in range(LOGIN_GUESSES.limit):
            client.post("/v1/auth/login", json={"password": "not it"})
        response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 429


@pytest.mark.database
def test_a_correct_login_does_not_spend_the_budget(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        for _ in range(LOGIN_GUESSES.limit * 2):
            assert client.post("/v1/auth/login", json={"password": PASSWORD}).status_code in (200, 201)
    assert not LOGIN_GUESSES.spent()


def test_schema_is_not_served_outside_local(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/schema/openapi.json")
    assert response.status_code == 404


@pytest.mark.database
def test_session_token_from_login_is_accepted_on_a_protected_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        login = client.post("/v1/auth/login", json={"password": PASSWORD})
        token = login.json()["access_token"]
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_expired_session_token_is_401_on_a_protected_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        token = AuthManager.issue_session_token("admin", ttl_seconds=-1)
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_cors_allowed_origins_are_configurable(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app()) as client:
        response = client.options(
            "/v1/clusters",
            headers={
                "Origin": "https://console.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") == "https://console.example.com"


def test_cors_rejects_an_unconfigured_origin(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app()) as client:
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
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/projects", json={"name": "x"}, headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert response.json()["detail"] == {"origin": "https://evil.example.com"}


def test_a_null_origin_is_refused():
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", json={"password": "x"}, headers={"Origin": "null"})
    assert response.status_code == 403


def test_a_write_from_an_allowed_or_the_serving_origin_passes_the_check(monkeypatch):
    enable_admin_auth(monkeypatch)
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://console.example.com")
    with TestClient(app=create_app()) as client:
        for origin in ("https://console.example.com", f"http://{client.base_url.netloc.decode()}"):
            response = client.post("/v1/auth/login", json={"password": "not it"}, headers={"Origin": origin})
            assert response.status_code == 401, origin


def test_a_write_with_a_non_json_body_is_415():
    """A cross-site form can post `text/plain` without a preflight; nothing here reads it."""
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", content=b'{"password": "x"}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_a_bodiless_write_needs_no_content_type(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
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


async def _run_hub_and(port: int, coro):
    stop: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    server_task = asyncio.create_task(serve_forever(stop))
    await asyncio.sleep(0.1)
    try:
        return await coro
    finally:
        stop.set_result(None)
        await server_task


@pytest.mark.asyncio
async def test_hub_rejects_a_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PORT", 18801)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_a_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PORT", 18802)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(
                f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}",
                additional_headers={"Authorization": "Bearer wrong-token"},
            ):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())


@pytest.mark.asyncio
async def test_hub_refuses_a_shared_fleet_token_before_the_upgrade(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PORT", 18803)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(
                f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}",
                additional_headers={"Authorization": "Bearer hangar-local-operator-token"},
            ):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_bad_tokens_even_when_admin_auth_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "disabled")
    monkeypatch.setattr(settings, "HUB_PORT", 18804)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())
