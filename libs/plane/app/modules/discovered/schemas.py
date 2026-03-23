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
    deployment_id: str | None
    spec_fingerprint: str | None
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


class PromoteRequest(Struct):
    """Turn a tracked sighting into a deployment the plane manages.

    `fingerprint` is required and is the whole safety of this call. The caller
    passes the digest they were shown; if the workload's pod template has changed
    since, the promotion is refused rather than pinned to a spec nobody looked at.
    It is `If-Match` for a thing the plane does not own.

    Scale is not a parameter. The bounds are taken from what the workload is
    already running, because a promotion that also decided the replica count would
    scale a live workload as a side effect of being catalogued.
    """

    recipe: str
    project: str
    fingerprint: str
    name: str | None = None
    priority_class: str | None = None
