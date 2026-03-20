import datetime as dt

from app.core.schemas import Struct


class DiscoveredWorkloadSchema(Struct):
    """One workload found running in a cluster we never declared it in.

    `status` is derived from the adoption row rather than stored on the sighting:
    the sighting is observed and has one writer, the decision is ours and has
    another, so the two are joined on read instead of sharing a column.
    """

    id: str
    cluster_id: str
    cluster_name: str
    namespace: str
    name: str
    kind: str
    model: str | None
    images: list[str]
    replicas_desired: int
    replicas_ready: int
    devices_per_replica: int
    devices_total: int
    node_ids: list[str]
    labels: dict
    status: str
    project_id: str | None
    note: str | None
    decided_by: str | None
    decided_at: dt.datetime | None
    first_seen_at: dt.datetime | None
    observed_at: dt.datetime | None


class DecisionRequest(Struct):
    """What to record about a sighting.

    There is no field here that could reach a cluster. Tracking a workload adds
    it to the inventory and assigns it an owner; it does not declare it, does not
    render a recipe against it, and does not emit `deployment.desired` — so an
    adopted model keeps the weights it already has resident.
    """

    project: str | None = None
    note: str | None = None
