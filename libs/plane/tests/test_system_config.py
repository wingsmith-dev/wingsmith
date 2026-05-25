"""`GET /v1/system/config` — the allowlist, and the guarantee it never leaks.

`test_no_configured_secret_appears_anywhere_in_the_response` is the reason this
suite exists: every credential in `Settings` is set to a distinctive sentinel and
the serialised body is searched for each one. A field added to `ConfigSchema` by
copying a secret across fails here rather than in a screenshot.
"""

import json

import pytest
import msgspec

from litestar.testing import TestClient

from app.config import settings
from app.api.app import create_app
from app.auth import AuthManager
from app.auth import AuthManager
from app.core.system_routes import effective_config


SECRETS = {
    "OPERATOR_TOKEN": "sentinel-operator-token-aaaa",
    "HF_TOKEN": "sentinel-hf-token-bbbb",
    "SESSION_SIGNING_SECRET": "sentinel-signing-secret-cccc",
    "DATABASE_URL": "postgresql+psycopg://dbuser:sentinel-db-password-dddd@db.internal:5433/hangar",
    "METRICS_SOURCES": (
        "default=https://prom.central:9090|token=sentinel-metrics-token-eeee,"
        "us|label=us-cluster-1,"
        "ap=https://prom.ap:9090|token=sentinel-metrics-token-ffff|kind=prometheus"
    ),
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD_HASH", AuthManager.hash_admin_password("sentinel-password-gggg"))
    monkeypatch.setattr(settings, "ADMIN_AUTH", "disabled")
    monkeypatch.setattr(settings, "APP_ENV", "staging")
    yield


def body() -> dict:
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/system/config")
    assert response.status_code == 200
    return response.json()


# --- the allowlist -----------------------------------------------------------


def test_response_carries_every_allowlisted_group():
    payload = body()
    assert set(payload) == {"app", "api", "hub", "reactor", "database", "metrics", "auth", "setup"}


def test_app_group_reports_the_resolved_environment_and_logging():
    app = body()["app"]
    assert app["env"] == "staging"
    assert app["log_level"] == settings.APP_LOG_LEVEL
    assert app["log_format"] == settings.APP_LOG_FORMAT
    assert isinstance(app["allowed_origins"], list)


def test_hub_group_reports_where_operators_dial_and_the_session_windows():
    hub = body()["hub"]
    assert hub["path"] == settings.HUB_PATH
    assert hub["port"] == settings.HUB_PORT
    assert hub["session_ttl_seconds"] == settings.HUB_SESSION_TTL_SECONDS
    assert hub["reaper_interval_seconds"] == settings.HUB_REAPER_INTERVAL_SECONDS


def test_reactor_and_setup_groups_report_their_diagnostic_fields():
    payload = body()
    assert payload["reactor"]["enabled"] == settings.REACTOR_ENABLED
    assert payload["setup"]["chart_ref"] == settings.SETUP_CHART_REF
    assert payload["setup"]["chart_private"] == settings.SETUP_CHART_PRIVATE


def test_database_is_reported_in_parts_and_never_as_a_url():
    database = body()["database"]
    assert database == {
        "driver": "postgresql+psycopg",
        "host": "db.internal",
        "port": 5433,
        "database": "hangar",
        "echo": settings.DB_ECHO,
        "pool_size": settings.DB_POOL_SIZE,
        "pool_max_overflow": settings.DB_POOL_MAX_OVERFLOW,
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
    }


def test_metrics_sources_are_parsed_with_their_cluster_label():
    metrics = body()["metrics"]
    by_cluster = {source["cluster"]: source for source in metrics["sources"]}
    assert set(by_cluster) == {"us", "ap"}
    assert by_cluster["us"]["url"] == "https://prom.central:9090"
    assert by_cluster["us"]["label"] == "us-cluster-1"
    assert by_cluster["ap"]["url"] == "https://prom.ap:9090"
    assert by_cluster["ap"]["label"] == ""
    assert all(set(source) == {"cluster", "url", "kind", "label"} for source in metrics["sources"])
    assert metrics["shared_url"] == "https://prom.central:9090"


def test_remote_write_reports_the_write_url_and_the_htpasswd_secret(monkeypatch):
    """Both are coordinates, not contents: a public ingress and a Secret's name."""
    monkeypatch.setattr(settings, "METRICS_REMOTE_WRITE_URL", "https://metrics.example/api/v1/write")
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", True)
    metrics = body()["metrics"]
    assert metrics["remote_write_url"] == "https://metrics.example/api/v1/write"
    assert metrics["remote_write_auth_secret"] == "platform/prom-remote-write-auth"


def test_remote_write_secret_is_empty_when_the_plane_does_not_maintain_it(monkeypatch):
    monkeypatch.setattr(settings, "REMOTE_WRITE_AUTH_ENABLED", False)
    assert effective_config().metrics.remote_write_auth_secret == ""


def test_shared_url_is_empty_when_no_default_endpoint_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_SOURCES", "us=https://prom.us:9090")
    metrics = effective_config().metrics
    assert metrics.shared_url == ""
    assert [source.cluster for source in metrics.sources] == ["us"]


def test_auth_group_reduces_every_credential_to_a_boolean():
    auth = body()["auth"]
    assert auth == {
        "admin_auth": "disabled",
        "admin_password_hash_set": True,
        "session_signing_secret_set": True,
        "session_ttl_seconds": settings.SESSION_TTL_SECONDS,
        "operator_token_set": True,
        "huggingface_token_set": True,
    }


def test_unset_credentials_report_false_rather_than_an_empty_string(monkeypatch):
    monkeypatch.setattr(settings, "HF_TOKEN", "")
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "")
    auth = effective_config().auth
    assert auth.huggingface_token_set is False
    assert auth.operator_token_set is False


def test_admin_auth_reports_the_resolved_mode_not_the_raw_setting(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "")
    assert effective_config().auth.admin_auth == "enabled"


# --- no secrets --------------------------------------------------------------


def test_no_configured_secret_appears_anywhere_in_the_response():
    """Every sentinel credential is searched for in the raw serialised body.

    Not a check on the fields we remembered to look at — a substring search over
    the whole payload, so a secret smuggled in under any new key fails here.
    """
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/system/config")
    raw = response.text
    for sentinel in (
        SECRETS["OPERATOR_TOKEN"],
        SECRETS["HF_TOKEN"],
        SECRETS["SESSION_SIGNING_SECRET"],
        "sentinel-db-password-dddd",
        "dbuser",
        "sentinel-metrics-token-eeee",
        "sentinel-metrics-token-ffff",
        "sentinel-password-gggg",
        settings.ADMIN_PASSWORD_HASH,
        SECRETS["DATABASE_URL"],
        SECRETS["METRICS_SOURCES"],
    ):
        assert sentinel not in raw, f"{sentinel!r} leaked into the config response"


def test_no_configured_secret_appears_in_the_struct_before_serialisation():
    """The same assertion against `effective_config`, so the projection is covered
    even if a transport ever re-shapes the response."""
    raw = msgspec.json.encode(effective_config()).decode()
    for sentinel in ("sentinel-db-password-dddd", "sentinel-metrics-token-eeee", SECRETS["OPERATOR_TOKEN"]):
        assert sentinel not in raw


def test_a_database_url_with_a_password_in_an_odd_shape_still_redacts(monkeypatch):
    monkeypatch.setattr(
        settings, "DATABASE_URL", "postgresql+psycopg://u:p%40ss%2Fword@10.0.0.5/hangar?sslmode=require"
    )
    database = body()["database"]
    assert database["host"] == "10.0.0.5"
    assert "p%40ss" not in json.dumps(database)
    assert "u:" not in json.dumps(database)


# --- auth --------------------------------------------------------------------


def test_config_requires_a_credential_when_admin_auth_is_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    with TestClient(app=create_app()) as client:
        response = client.get("/v1/system/config")
    assert response.status_code == 401


def test_config_accepts_an_admin_session_token(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    with TestClient(app=create_app()) as client:
        token = AuthManager.issue_session_token("admin")
        response = client.get("/v1/system/config", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_config_refuses_an_operator_token():
    with TestClient(app=create_app()) as client:
        response = client.get(
            "/v1/system/config",
            headers={"Authorization": f"Bearer {SECRETS['OPERATOR_TOKEN']}"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
