from litestar.testing import create_test_client

from app.config import settings
from app.core.auth import STATIC_OPERATOR
from app.core.auth import resolve_principal
from app.core.routes import HealthRoutes


def test_settings_default_database_url() -> None:
    assert settings.DATABASE_URL.startswith("postgresql+psycopg://")


def test_every_caller_resolves_to_the_static_operator() -> None:
    assert resolve_principal(None) == STATIC_OPERATOR
    assert resolve_principal(settings.OPERATOR_TOKEN) == STATIC_OPERATOR


def test_health() -> None:
    with create_test_client(route_handlers=[HealthRoutes]) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": True}
