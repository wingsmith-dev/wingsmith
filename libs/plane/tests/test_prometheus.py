"""The API's own `/metrics`: served without a credential, labelled by route template."""

import re

import pytest

from litestar.testing import TestClient

from app.config import settings
from app.api.app import create_app
from app.api.metrics import REGISTRY


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_AUTH", "enabled")
    monkeypatch.setattr(settings, "SESSION_SIGNING_SECRET", "metrics-signing-secret")
    with TestClient(app=create_app()) as http:
        yield http


def requests_total(method: str, route: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "wingsmith_plane_http_requests_total", {"method": method, "route": route, "status": status}
    )
    return value or 0.0


def test_metrics_needs_no_credential_and_is_the_prometheus_text_format(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert re.search(r'^wingsmith_plane_build_info\{version="[^"]+"\} 1\.0$', response.text, re.M)
    assert "# TYPE wingsmith_plane_http_request_duration_seconds histogram" in response.text


def test_a_scrape_with_query_params_is_not_refused(client):
    assert client.get("/metrics?module=plane").status_code == 200


def test_a_request_is_counted_under_its_status(client):
    before = requests_total("GET", "/health", "200")
    assert client.get("/health").status_code == 200
    assert requests_total("GET", "/health", "200") == before + 1


def test_a_refused_request_is_counted_under_the_route_template_not_the_path(client):
    route = "/v1/clusters/{ref}"
    before = requests_total("GET", route, "401")
    assert client.get("/v1/clusters/clu_does_not_matter").status_code == 401
    assert requests_total("GET", route, "401") == before + 1
    assert "clu_does_not_matter" not in client.get("/metrics").text


def test_nothing_is_left_in_flight_once_a_request_has_answered(client):
    client.get("/health")
    assert REGISTRY.get_sample_value("wingsmith_plane_http_requests_in_flight") == 0
