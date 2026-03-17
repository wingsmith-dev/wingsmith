import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class ClusterSyncSchema(Struct):
    """The observed liveness of a cluster's agent link.

    `state` is derived, not stored: a cluster that has never connected is not the
    same as one that connected and dropped, and neither is a value anybody
    declared.
    """

    state: str
    last_sync_at: dt.datetime | None
    last_seen_at: dt.datetime | None
    agent_version: str | None
    lag_ms: int | None
    last_error: str | None


class ClusterSchema(Struct):
    """Declared and observed returned together, never merged.

    `phase` is what we asked for; `reachable` and `sync` are what is true right
    now. Folding the two would make "someone disabled this cluster" and "the
    network is down" the same fact, and they need different responses.
    """

    id: str
    name: str
    display_name: str | None
    region: str
    zone: str | None
    residency: str | None
    phase: str
    labels: dict
    capabilities: dict
    enrolled_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    reachable: bool
    sync: ClusterSyncSchema


class ClusterCreateRequest(Struct):
    name: str
    region: str
    display_name: str | None = None
    zone: str | None = None
    residency: str | None = None
    phase: str = "pending"
    labels: dict = {}
    capabilities: dict = {}


class ClusterUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears."""

    display_name: str | None | UnsetType = UNSET
    region: str | UnsetType = UNSET
    zone: str | None | UnsetType = UNSET
    residency: str | None | UnsetType = UNSET
    phase: str | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    capabilities: dict | UnsetType = UNSET
