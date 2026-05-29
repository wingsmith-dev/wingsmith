from app.core.schemas import Struct


class PlacementPreviewRequest(Struct):
    """A placement question asked without creating anything.

    The same three inputs a deployment supplies — what to run, who for, and at
    what priority — because a preview that took a different shape from the real
    call would be a second scheduler to keep in step.
    """

    recipe: str
    project: str
    priority_class: str | None = None
    replicas: int = 1


class PlacementExpandRequest(Struct):
    """The split an existing workload would get, asked without writing a row.

    Takes a workload rather than a recipe because a split is a workload question:
    the totals, the strategy, the floors and the fan-out cap all live there, and a
    preview built from anything less would answer about a different workload.
    """

    workload: str


class ReservationSchema(Struct):
    """Capacity a cluster owes a deployment that has not bound a device yet.

    Derived on read and stored nowhere. It is the difference between what a
    deployment declares — this cluster's share, times the recipe's device shape —
    and what ingestion has confirmed the cluster actually bound, so it appears the
    moment placement chooses a cluster and reaches zero on its own as the
    allocations arrive.

    Deliberately not an `AllocationSchema` and deliberately not on that route. An
    allocation is observed and names its devices; this is declared and names none,
    because which accelerators the promise lands on is the cluster's answer and it
    has not given it. Rendering the two as one list would tell an operator we know
    something we do not.
    """

    cluster: str
    cluster_name: str
    project: str
    workload: str
    workload_name: str | None
    deployment: str | None
    deployment_name: str | None
    device_class: str
    devices: int
    requires_single_domain: bool
    domain_kind: str | None
