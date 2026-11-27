import datetime as dt

from typing import Literal

from app.core.schemas import Struct


MediaJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class MediaJobCreateRequest(Struct):
    """What a gateway submits for a caller (ADR 0041). `tenant` and `endpoint` are uids the gateway
    resolved from the caller's key and the request's `model`; the project is the endpoint's."""

    tenant: str
    endpoint: str
    kind: str
    path: str
    model: str
    request: dict
    idempotency_key: str | None = None


class MediaJobSchema(Struct):
    id: str
    tenant: str
    project: str
    endpoint: str | None
    kind: str
    path: str
    model: str
    status: MediaJobStatus
    progress: int
    attempts: int
    cancel_requested: bool
    created_at: dt.datetime
    started_at: dt.datetime | None
    completed_at: dt.datetime | None
    expires_at: dt.datetime | None
    object_key: str | None
    content_type: str | None
    size_bytes: int | None
    upstream_seconds: float | None
    units: dict | None
    error: dict | None
    request: dict | None = None
    """Only on a claim: the body the worker replays upstream."""


class MediaJobClaimRequest(Struct):
    worker: str
    kinds: list[str] | None = None


class MediaJobHeartbeatRequest(Struct):
    worker: str
    lease: int
    """The claim's `attempts`: it fences a stale claim off from the one after it, even on one worker."""
    progress: int | None = None


class MediaJobHeartbeatSchema(Struct):
    cancel_requested: bool
    lease_expires_at: dt.datetime


class MediaJobCompleteRequest(Struct):
    worker: str
    lease: int
    status: Literal["succeeded", "failed", "cancelled"]
    object_key: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    upstream_seconds: float | None = None
    units: dict | None = None
    error: dict | None = None
