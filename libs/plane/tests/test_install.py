"""The operator install one-liner served by the hub, and its bootstrap token."""

import http

from app.hub import install
from app.config import settings
from app.core.auth import INSTALL_PREFIX
from app.core.auth import issue_install_token
from app.core.auth import verify_install_token


class FakeConnection:
    """Stands in for a `websockets` connection: `respond` returns its arguments."""

    def respond(self, status, body):
        return (status, body)


def test_install_token_round_trips_to_its_cluster() -> None:
    token = issue_install_token("cluster-ap", ttl_seconds=60)
    assert token.startswith(f"{INSTALL_PREFIX}.")
    assert verify_install_token(token) == "cluster-ap"


def test_expired_install_token_is_rejected() -> None:
    assert verify_install_token(issue_install_token("c1", ttl_seconds=-1)) is None


def test_a_session_token_is_not_accepted_as_an_install_token() -> None:
    from app.core.auth import issue_session_token

    assert verify_install_token(issue_session_token("admin")) is None


def test_tampered_install_token_is_rejected() -> None:
    token = issue_install_token("c1", ttl_seconds=60)
    prefix, payload, signature = token.split(".")
    forged = f"{prefix}.{payload}.{signature[:-1]}{'a' if signature[-1] != 'a' else 'b'}"
    assert verify_install_token(forged) is None


def test_rendered_manifest_carries_the_cluster_image_hub_and_credential(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPERATOR_IMAGE", "reg/hangar-operator:abc123")
    monkeypatch.setattr(settings, "PUBLIC_HOST", "hangar.example.com")
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "")
    body = install.render_manifest("cluster-ap", "ignored-host")
    assert "HANGAR_CLUSTER_ID: cluster-ap" in body
    assert "image: reg/hangar-operator:abc123" in body
    assert "wss://hangar.example.com/v1/hub/sync" in body
    assert "enrolment-token:" in body
    assert "${" not in body


def test_manifest_is_unavailable_without_a_pinned_image(monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPERATOR_IMAGE", "")
    token = issue_install_token("c1", ttl_seconds=60)
    status, _ = install.install_response(FakeConnection(), f"/install/{token}/operator.yaml", "h")
    assert status == http.HTTPStatus.SERVICE_UNAVAILABLE


def test_script_points_at_the_manifest_url_and_carries_no_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_HOST", "hangar.example.com")
    token = issue_install_token("c1", ttl_seconds=60)
    status, body = install.install_response(FakeConnection(), f"/install/{token}.sh", "h")
    assert status == http.HTTPStatus.OK
    assert f"https://hangar.example.com/install/{token}/operator.yaml" in body
    assert "kubectl apply -f -" in body
    assert (settings.OPERATOR_TOKEN or "hangar-local-operator-token") not in body


def test_bad_token_is_unauthorized_and_non_install_paths_pass_through() -> None:
    status, _ = install.install_response(FakeConnection(), "/install/not-a-token.sh", "h")
    assert status == http.HTTPStatus.UNAUTHORIZED
    assert install.install_response(FakeConnection(), "/v1/hub/sync", "h") is None
