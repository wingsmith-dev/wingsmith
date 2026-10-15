"""The console, served from the API process.

A console URL's query string is the client-side router's (`/clusters?phase=active`),
so the API's unknown-parameter check must not see it; it answered every such
reload with a 400 JSON body. Every file it serves carries the headers a page
holding an admin session should.
"""

import pytest

from litestar import get
from litestar.testing import create_test_client

from app.api import spa
from app.api.spa import ConsoleRoutes
from app.core.params import reject_unknown_params
from app.core.handlers import EXCEPTION_HANDLERS


@get("/v1/things", sync_to_thread=False)
def things(limit: int | None = None) -> dict:
    return {"limit": limit}


@pytest.fixture
def http(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(spa, "STATIC_DIR", tmp_path)
    with create_test_client(
        route_handlers=[ConsoleRoutes, things],
        before_request=reject_unknown_params,
        exception_handlers=EXCEPTION_HANDLERS,
    ) as client:
        yield client


@pytest.mark.parametrize("url", ["/?tab=sync", "/clusters?phase=active&region=eu-west-1", "/tenants/acme?edit=1"])
def test_a_console_url_with_a_query_string_serves_the_console(http, url):
    response = http.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "id=root" in response.text


def test_an_api_route_still_refuses_an_unknown_parameter(http):
    assert http.get("/v1/things?limit=1").status_code == 200
    response = http.get("/v1/things?limt=1")
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_parameter"


@pytest.mark.parametrize("url", ["/", "/clusters", "/assets/app.js"])
def test_every_console_response_carries_the_security_headers(http, url):
    headers = http.get(url).headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    policy = headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-eval" not in policy
