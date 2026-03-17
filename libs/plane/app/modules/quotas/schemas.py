import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class QuotaUsageSchema(Struct):
    """The observed half of a quota: what the project is actually holding.

    Counted over live allocations at read time. It sits in its own object so that
    no field on the response is ever half declared and half observed — a single
    `used_devices` next to `guaranteed_devices` would read as one number someone
    set and one number that happened, spelled identically.
    """

    allocated_devices: int
    live_allocations: int
    over_quota_devices: int


class QuotaSchema(Struct):
    """A guaranteed floor and a fair share, with usage beside it, never merged.

    `cluster` is null when the quota spans every cluster, and usage is then
    summed across all of them. `over_limit` and `headroom_devices` are derived
    here rather than in each client, because the console and `hgr` asking the
    same question must not get two answers.
    """

    id: str
    project: str
    project_slug: str
    cluster: str | None
    cluster_name: str | None
    device_class: str
    guaranteed_devices: int
    limit_devices: int | None
    over_quota_weight: float
    over_quota_enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    usage: QuotaUsageSchema
    headroom_devices: int | None
    over_limit: bool


class QuotaCreateRequest(Struct):
    project: str
    device_class: str
    cluster: str | None = None
    guaranteed_devices: int = 0
    limit_devices: int | None = None
    over_quota_weight: float = 1.0
    over_quota_enabled: bool = True


class QuotaUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `project`, `cluster` and `device_class` are absent by design: the three of
    them are the quota's unique scope, and changing one is creating a different
    quota rather than editing this one.
    """

    guaranteed_devices: int | UnsetType = UNSET
    limit_devices: int | None | UnsetType = UNSET
    over_quota_weight: float | UnsetType = UNSET
    over_quota_enabled: bool | UnsetType = UNSET
