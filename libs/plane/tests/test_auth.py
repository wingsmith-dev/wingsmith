"""Auth: the operator/admin seam in `app.core.auth`, its wiring into the HTTP
API (`app.core.context`, `app.core.auth_routes`) and the hub
(`app.hub.server`).
"""

import asyncio

import pytest

from litestar.testing import TestClient
from websockets.exceptions import InvalidStatus
from websockets.asyncio.client import connect

from app.config import settings
from app.api.app import create_app
from app.core.auth import LOCAL_OPERATOR_TOKEN
from app.core.auth import hash_password
from app.core.auth import verify_password
from app.core.auth import resolve_operator
from app.core.auth import resolve_principal
from app.core.auth import admin_auth_enabled
from app.core.auth import operator_principal
from app.core.auth import issue_session_token
from app.core.auth import hash_enrolment_token
from app.core.auth import looks_like_enrolment
from app.core.auth import mint_enrolment_token
from app.core.auth import verify_session_token
from app.hub.server import serve_forever
from app.hub.server import _resolve_connection
from app.config.settings import Settings
from app.config.settings import validate
from app.config.settings import resolved_admin_auth
from app.core.exceptions import NotAuthorized


PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _reset_auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "")
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "")
    monkeypatch.setattr(settings, "SESSION_TTL_SECONDS", 900)
    yield


def enable_admin_auth(monkeypatch, password: str = PASSWORD) -> None:
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", hash_password(password))
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "test-signing-secret")


# --- password hashing -------------------------------------------------------


def test_password_hash_round_trips():
    digest = hash_password(PASSWORD)
    assert digest != PASSWORD
    assert verify_password(PASSWORD, digest)


def test_wrong_password_does_not_verify():
    digest = hash_password(PASSWORD)
    assert not verify_password("wrong password", digest)


def test_empty_hash_never_verifies():
    assert not verify_password(PASSWORD, "")


# --- session tokens ----------------------------------------------------------


def test_session_token_round_trips(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = issue_session_token("admin")
    principal = verify_session_token(token)
    assert principal is not None
    assert principal.kind == "admin"


def test_expired_session_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = issue_session_token("admin", ttl_seconds=-1)
    assert verify_session_token(token) is None


def test_tampered_session_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = issue_session_token("admin")
    prefix, payload, signature = token.split(".")
    tampered = f"{prefix}.{payload}.{signature[:-1]}{'a' if signature[-1] != 'a' else 'b'}"
    assert verify_session_token(tampered) is None


def test_tampered_session_payload_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit")
    token = issue_session_token("admin")
    prefix, payload, signature = token.split(".")
    forged = f"{prefix}.{payload}extra.{signature}"
    assert verify_session_token(forged) is None


def test_malformed_session_token_is_rejected():
    assert verify_session_token("not-a-session-token") is None
    assert verify_session_token("hgrs1.onlyonepart") is None


def test_session_token_signed_under_a_different_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-a")
    token = issue_session_token("admin")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "sekrit-b")
    assert verify_session_token(token) is None


# --- resolve_operator / resolve_principal ------------------------------------


def test_resolve_operator_accepts_the_local_default_in_local_env(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "local")
    assert resolve_operator(LOCAL_OPERATOR_TOKEN) is not None


def test_resolve_operator_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "the-real-token")
    assert resolve_operator("some-other-token") is None


def test_resolve_operator_rejects_no_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "the-real-token")
    assert resolve_operator(None) is None
    assert resolve_operator("") is None


def test_resolve_operator_accepts_the_configured_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "the-real-token")
    principal = resolve_operator("the-real-token")
    assert principal is not None
    assert principal.kind == "operator"


def test_resolve_principal_with_admin_auth_disabled_accepts_no_token():
    principal = resolve_principal(None)
    assert principal.kind == "admin"


def test_resolve_principal_with_admin_auth_enabled_rejects_no_token(monkeypatch):
    enable_admin_auth(monkeypatch)
    with pytest.raises(NotAuthorized):
        resolve_principal(None)


def test_resolve_principal_with_admin_auth_enabled_rejects_wrong_token(monkeypatch):
    enable_admin_auth(monkeypatch)
    with pytest.raises(NotAuthorized):
        resolve_principal("garbage")


def test_resolve_principal_accepts_a_session_token_regardless_of_admin_auth_mode(monkeypatch):
    enable_admin_auth(monkeypatch)
    token = issue_session_token("admin")
    principal = resolve_principal(token)
    assert principal.kind == "admin"


def test_resolve_principal_prefers_operator_token_over_admin_bypass(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "op-token")
    principal = resolve_principal("op-token")
    assert principal.kind == "operator"


# --- admin auth mode resolution / settings.validate --------------------------


def test_admin_auth_defaults_disabled_in_local():
    s = Settings(APP_ENV="local")
    assert resolved_admin_auth(s) == "disabled"
    assert not admin_auth_enabled() or settings.APP_ENV != "local"


def test_admin_auth_defaults_enabled_outside_local():
    s = Settings(APP_ENV="production")
    assert resolved_admin_auth(s) == "enabled"


def test_admin_auth_explicit_flag_wins_over_the_default():
    s = Settings(APP_ENV="production", ADMIN_AUTH="disabled")
    assert resolved_admin_auth(s) == "disabled"


def test_validate_refuses_to_boot_without_operator_token_outside_local():
    s = Settings(APP_ENV="production", OPERATOR_TOKEN="")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_refuses_to_boot_admin_enabled_without_password_hash():
    s = Settings(APP_ENV="production", OPERATOR_TOKEN="tok", ADMIN_AUTH="enabled", SESSION_SIGNING_SECRET="sig")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_refuses_to_boot_admin_enabled_without_signing_secret():
    s = Settings(APP_ENV="production", OPERATOR_TOKEN="tok", ADMIN_AUTH="enabled", ADMIN_PASSWORD_HASH="hash")
    with pytest.raises(RuntimeError):
        validate(s)


def test_validate_passes_when_admin_auth_is_explicitly_disabled_outside_local():
    s = Settings(APP_ENV="production", OPERATOR_TOKEN="tok", ADMIN_AUTH="disabled")
    validate(s)


def test_validate_is_frictionless_in_local_with_nothing_configured():
    validate(Settings(APP_ENV="local"))


# --- HTTP API wiring -----------------------------------------------------


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


def test_correct_operator_token_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "op-token")
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/clusters", headers={"Authorization": "Bearer op-token"})
    assert response.status_code == 200


def test_admin_login_succeeds_with_the_right_password(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 201 or response.status_code == 200
    body = response.json()
    assert "token" in body and body["expires_in"] > 0


def test_admin_login_fails_with_the_wrong_password(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        response = client.post("/v1/auth/login", json={"password": "not it"})
    assert response.status_code == 401
    assert response.json()["code"] == "not_authorized"


def test_session_token_from_login_is_accepted_on_a_protected_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        login = client.post("/v1/auth/login", json={"password": PASSWORD})
        token = login.json()["token"]
        response = client.get("/v1/clusters", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_expired_session_token_is_401_on_a_protected_route(monkeypatch):
    enable_admin_auth(monkeypatch)
    with TestClient(app=create_app()) as client:
        token = issue_session_token("admin", ttl_seconds=-1)
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


# --- enrolment tokens ----------------------------------------------------


def test_mint_enrolment_token_is_prefixed_and_hashes_to_its_digest():
    token, digest = mint_enrolment_token()
    assert token.startswith("hgro1.")
    assert digest == hash_enrolment_token(token)
    assert len(digest) == 64


def test_minted_enrolment_tokens_are_unique():
    a, ha = mint_enrolment_token()
    b, hb = mint_enrolment_token()
    assert a != b and ha != hb


def test_looks_like_enrolment_only_accepts_the_prefix():
    token, _ = mint_enrolment_token()
    assert looks_like_enrolment(token)
    assert not looks_like_enrolment(None)
    assert not looks_like_enrolment("")
    assert not looks_like_enrolment("some-fleet-token")


def test_bound_operator_principal_carries_the_cluster_in_scope():
    bound = operator_principal(cluster_uid="clu_123", cluster_name="ap")
    assert bound.scope == "clu_123"
    assert bound.name == "ap"
    assert operator_principal().scope is None


@pytest.mark.asyncio
async def test_resolve_connection_returns_the_unbound_principal_for_the_fleet_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "fleet-token")
    principal = await _resolve_connection("fleet-token")
    assert principal is not None
    assert principal.kind == "operator"
    assert principal.scope is None


@pytest.mark.asyncio
async def test_resolve_connection_refuses_a_token_that_is_neither_fleet_nor_enrolment(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "fleet-token")
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
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "hub-secret")
    monkeypatch.setattr(settings, "HUB_PORT", 18801)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_a_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "hub-secret")
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
async def test_hub_accepts_the_correct_operator_token(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "hub-secret")
    monkeypatch.setattr(settings, "HUB_PORT", 18803)

    async def attempt():
        async with connect(
            f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}",
            additional_headers={"Authorization": "Bearer hub-secret"},
        ) as ws:
            assert ws.state.value == 1

    await _run_hub_and(settings.HUB_PORT, attempt())


@pytest.mark.asyncio
async def test_hub_rejects_bad_tokens_even_when_admin_auth_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "hub-secret")
    monkeypatch.setattr(settings, "ADMIN_AUTH", "disabled")
    monkeypatch.setattr(settings, "HUB_PORT", 18804)

    async def attempt():
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://127.0.0.1:{settings.HUB_PORT}{settings.HUB_PATH}"):
                pass
        assert excinfo.value.response.status_code == 401

    await _run_hub_and(settings.HUB_PORT, attempt())
