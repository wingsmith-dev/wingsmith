"""The media job surface's access rules and the object store's signing (ADR 0041)."""

import datetime as dt

import pytest

from app.auth import AuthManager
from app.core.context import authorize
from app.core.exceptions import Forbidden
from app.integrations.objectstore import sign_v4
from app.integrations.objectstore import media_store


def test_only_the_gateway_reaches_the_media_job_queue():
    gateway = AuthManager.gateway_principal()
    authorize(gateway, "POST", "/v1/gateway/media-jobs")
    authorize(gateway, "POST", "/v1/gateway/media-jobs/claim")
    authorize(gateway, "GET", "/v1/gateway/media-jobs/{uid:str}")
    with pytest.raises(Forbidden):
        authorize(gateway, "POST", "/v1/gateway/media-jobs-other")
    with pytest.raises(Forbidden):
        authorize(AuthManager.admin_principal(), "GET", "/v1/gateway/media-jobs/{uid:str}")
    with pytest.raises(Forbidden):
        authorize(AuthManager.tenant_principal(tenant_uid="ten_1", slug="acme"), "GET", "/v1/gateway/media-jobs")


def test_a_delete_is_signed_with_sigv4():
    now = dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.UTC)
    headers = sign_v4("DELETE", "s3.local:9000", "/bucket/media/p/j", "us-east-1", "AK", "SK", now)
    assert headers["x-amz-date"] == "20261127T120000Z"
    auth = headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AK/20261127/us-east-1/s3/aws4_request, ")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    # Deterministic, and bound to the path.
    assert headers == sign_v4("DELETE", "s3.local:9000", "/bucket/media/p/j", "us-east-1", "AK", "SK", now)
    assert headers != sign_v4("DELETE", "s3.local:9000", "/bucket/media/p/k", "us-east-1", "AK", "SK", now)


def test_no_store_without_an_endpoint_and_bucket(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MEDIA_S3_ENDPOINT", "")
    assert media_store() is None
    monkeypatch.setattr(settings, "MEDIA_S3_ENDPOINT", "http://s3.local")
    monkeypatch.setattr(settings, "MEDIA_S3_BUCKET", "media")
    assert media_store() is not None
