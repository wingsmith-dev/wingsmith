import datetime as dt

from app.core.schemas import Struct


class AllocationDeviceSchema(Struct):
    """One device inside one allocation.

    Released members are returned rather than filtered out: the row is how a
    finished allocation stays auditable, and `live` says which side of that line
    a member is on without the client having to compare timestamps.
    """

    device: str
    device_uuid: str
    node: str
    live: bool
    released_at: dt.datetime | None


class AllocationSchema(Struct):
    """Observed: a set of devices committed together to one workload.

    Every field here is written by ingestion. Allocation is made by placement and
    reported by the cluster, so there is no declared half to sit beside — the
    closest thing is the workload's request, and that lives on the workload.

    `gang_complete` is the one derived field: the allocation is the unit of
    commitment, so holding fewer live devices than `device_count` is a partially
    satisfied gang, and that is a different situation from being released.
    """

    id: str
    cluster: str
    cluster_name: str
    project: str
    project_slug: str
    workload: str
    replica: str | None
    device_class: str
    device_count: int
    over_quota: bool
    phase: str
    live: bool
    gang_complete: bool
    acquired_at: dt.datetime | None
    released_at: dt.datetime | None
    observed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    devices: list[AllocationDeviceSchema]
