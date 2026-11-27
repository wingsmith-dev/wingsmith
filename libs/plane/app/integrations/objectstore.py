"""The plane's half of media object storage (ADR 0041): deleting expired outputs.

The gateway writes and presigns; the plane only deletes, so this is one SigV4-signed `DELETE`
against an S3-compatible endpoint, path-style, without an SDK.
"""

import hmac
import hashlib
import datetime as dt

from urllib.parse import quote

import httpx

from app.config import settings


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(
    method: str, host: str, path: str, region: str, access_key: str, secret_key: str, now: dt.datetime
) -> dict[str, str]:
    """Headers for a SigV4-signed request with an empty body."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed, EMPTY_SHA256])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": stamp,
        "authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}",
    }


class S3ObjectStore:
    def __init__(self, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key

    async def delete(self, key: str) -> None:
        url = httpx.URL(self.endpoint)
        path = "/" + quote(self.bucket) + "/" + quote(key, safe="/")
        host = url.host if url.port is None else f"{url.host}:{url.port}"
        headers = sign_v4("DELETE", host, path, self.region, self.access_key, self.secret_key, dt.datetime.now(dt.UTC))
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(f"{self.endpoint}{path}", headers=headers)
        # 404 is a delete that already happened.
        if response.status_code not in (200, 204, 404):
            raise RuntimeError(f"object store refused DELETE {key}: {response.status_code}")


def media_store() -> S3ObjectStore | None:
    """The configured media store, or None when none is configured."""
    if not (settings.MEDIA_S3_ENDPOINT and settings.MEDIA_S3_BUCKET):
        return None
    return S3ObjectStore(
        settings.MEDIA_S3_ENDPOINT,
        settings.MEDIA_S3_BUCKET,
        settings.MEDIA_S3_REGION,
        settings.MEDIA_S3_ACCESS_KEY_ID,
        settings.MEDIA_S3_SECRET_ACCESS_KEY,
    )
