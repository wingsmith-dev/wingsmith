"""dev/seed.py: what it sends is what the plane accepts, and a refusal stops it.

Runs in the plane's virtualenv so every body is decoded by the plane's own request struct, which
forbids unknown fields. A body for a route that no longer exists, or a field that was renamed,
fails here, not as a 404 or 400 halfway through somebody's first `just seed --demo`.
"""

from __future__ import annotations

import json
import threading

from typing import Any
from collections import Counter
from http.server import HTTPServer
from http.server import BaseHTTPRequestHandler

import seed
import fleet
import pytest
import msgspec

from app.modules.pools.schemas import PoolCreateRequest
from app.modules.quotas.schemas import QuotaCreateRequest
from app.modules.tenants.schemas import TenantCreateRequest
from app.modules.clusters.schemas import ClusterCreateRequest
from app.modules.policies.schemas import PolicyCreateRequest
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.artifacts.schemas import ArtifactCreateRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.priority_classes.schemas import PriorityClassCreateRequest


REQUESTS = {
    "/tenants": TenantCreateRequest,
    "/clusters": ClusterCreateRequest,
    "/pools": PoolCreateRequest,
    "/priority-classes": PriorityClassCreateRequest,
    "/projects": ProjectCreateRequest,
    "/quotas": QuotaCreateRequest,
    "/artifacts": ArtifactCreateRequest,
    "/policies": PolicyCreateRequest,
    "/workloads": WorkloadCreateRequest,
    "/endpoints": EndpointCreateRequest,
}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    posts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(seed, "post", lambda path, body: posts.append((path, body)))
    monkeypatch.setattr(seed, "call", lambda method, path, body=None: {"items": []})
    return posts


def test_every_demo_body_decodes_as_the_plane_request_it_is_sent_to(sent) -> None:
    seed.seed_tenants()
    seed.seed_declared(fleet.FLEET)
    seed.seed_demand()

    assert {path for path, _ in sent} == set(REQUESTS)
    for path, body in sent:
        msgspec.convert(body, REQUESTS[path])


def test_demo_workloads_fit_the_fleet_and_their_quota(sent) -> None:
    devices: Counter[tuple[str, str]] = Counter()
    for spec in fleet.FLEET:
        for node in spec.nodes:
            devices[(spec.name, node.device_class)] += node.devices
    artifacts = {row[0] for row in seed.ARTIFACTS}
    quotas = {(project, device_class): (cluster, limit) for project, device_class, cluster, _, limit in seed.QUOTAS}

    for name, cluster, weights, device_class, tensor, _, _, project, _, desired, _, _ in seed.WORKLOADS:
        assert weights in artifacts, name
        assert (project, device_class) in quotas, f"{name}: no {device_class} quota in {project}"
        quota_cluster, limit = quotas[(project, device_class)]
        assert desired * tensor <= limit, f"{name}: {desired}x{tensor} devices exceed its quota"
        clusters = [c for c, k in devices if k == device_class and (cluster or quota_cluster or c) == c]
        assert any(devices[(c, device_class)] >= desired * tensor for c in clusters), f"{name}: no cluster fits it"


def test_pools_cover_every_node_the_fleet_reports() -> None:
    declared = {(pool["cluster"], pool["name"]) for pool in seed.pools(fleet.FLEET)}
    reported = {(spec.name, node.pool) for spec in fleet.FLEET for node in spec.nodes}
    assert declared == reported


class Recorder(BaseHTTPRequestHandler):
    status = 201
    headers_seen: list[dict[str, str]] = []

    def do_POST(self) -> None:
        Recorder.headers_seen.append(dict(self.headers))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"code": "conflict"} if self.status == 409 else {"id": "x"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(fleet, "API", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(fleet, "API_TOKEN", "")
    Recorder.headers_seen = []
    yield Recorder
    server.shutdown()


def test_post_returns_what_was_created(api) -> None:
    api.status = 201
    assert seed.post("/tenants", {"slug": "acme"}) == {"id": "x"}


def test_post_treats_an_existing_object_as_done(api) -> None:
    api.status = 409
    assert seed.post("/tenants", {"slug": "acme"}) is None


@pytest.mark.parametrize("status", [400, 404, 422, 500])
def test_post_stops_on_any_other_refusal(api, status: int) -> None:
    api.status = status
    with pytest.raises(fleet.ApiError) as raised:
        seed.post("/tenants", {"slug": "acme"})
    assert raised.value.status == status


def test_main_exits_non_zero_when_the_api_refuses(api, capsys: pytest.CaptureFixture[str]) -> None:
    api.status = 500
    assert seed.main([]) == 1
    assert "500" in capsys.readouterr().err


def test_api_token_is_sent_as_a_bearer_when_set(api, monkeypatch: pytest.MonkeyPatch) -> None:
    api.status = 201
    monkeypatch.setattr(fleet, "API_TOKEN", "session-token")
    seed.post("/tenants", {"slug": "acme"})
    assert api.headers_seen[-1].get("Authorization") == "Bearer session-token"


def test_no_api_token_sends_no_authorization(api) -> None:
    api.status = 201
    seed.post("/tenants", {"slug": "acme"})
    assert "Authorization" not in api.headers_seen[-1]
