from litestar.testing import create_test_client

from app.auth import STATIC_OPERATOR
from app.auth import AuthManager
from app.config import settings
from app.core.routes import HealthRoutes
from app.core.exceptions import NotAuthorized


def test_settings_default_database_url() -> None:
    assert settings.DATABASE_URL.startswith("postgresql+psycopg://")


def test_operator_token_resolves_to_the_operator_principal() -> None:
    assert AuthManager.resolve_principal(settings.OPERATOR_TOKEN or "hangar-local-operator-token") == STATIC_OPERATOR


def test_no_token_resolves_under_local_defaults_because_admin_auth_is_disabled() -> None:
    assert AuthManager.resolve_principal(None).kind == "admin"


def test_wrong_token_is_rejected_when_admin_auth_would_be_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    try:
        raised = False
        try:
            AuthManager.resolve_principal("not-a-real-token")
        except NotAuthorized:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(settings, "ADMIN_AUTH", "")


def test_health() -> None:
    with create_test_client(route_handlers=[HealthRoutes]) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": True}
