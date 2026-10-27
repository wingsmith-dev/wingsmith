from app.core.schemas import Struct


class PlacementShapeRequest(Struct):
    """A device shape stated directly, for asking about something that does not exist.

    Field-for-field the shape a workload carries, and spelled with the workload's
    own column names so that one builder can read either without translating. That
    is deliberate: a preview that described a shape differently from the object it
    previews would be a second vocabulary to keep in step, and the first divergence
    would be a preview that answers about something the create then refuses.

    Nothing here is stored and nothing is validated against an artifact: this is a
    question, not a declaration. `check_shape` still governs what may be created.
    """

    device_class: str
    device_count: int = 1
    device_memory_mib_min: int = 0
    tensor_parallel: int = 1
    requires_single_topology_domain: bool = False
    topology_domain_kind: str | None = None


class PlacementPreviewRequest(Struct):
    """A placement question asked without creating anything.

    The same inputs a deployment supplies, what to run, who for, and at what
    priority, because a preview that took a different shape from the real call
    would be a second scheduler to keep in step.

    Exactly one of `workload` and `shape` says what to place, and the pair is the
    point of this object. `workload` asks about something that exists. `shape` asks
    about something that does not, which is the question a create form has: the
    person is choosing a fit and wants the plane's verdict BEFORE they commit, not
    after.

    That second form is a capability this endpoint briefly lost. It used to name a
    recipe, and a recipe was an object you could declare and ask about before any
    workload referenced it; when the recipe went away the only askable thing left
    was a workload that, on a create form, does not exist yet. Instead of restoring
    an object to be the subject of a question, the question takes the shape
    directly, which is what it always wanted, and is why the recipe could answer
    it in the first place.

    `project` is required in both forms. Capacity is read per project, so there is
    no project-independent answer to give.
    """

    project: str
    workload: str | None = None
    shape: PlacementShapeRequest | None = None
    priority_class: str | None = None
    replicas: int = 1


class PlacementExpandRequest(Struct):
    """The split an existing workload would get, asked without writing a row.

    Separate from `PlacementPreviewRequest` because a split is a different
    question: it reads the totals, the strategy, the floors and the fan-out cap
    exactly as declared, where a preview overrides project, priority and scale to
    ask what a shape would cost somewhere it is not.
    """

    workload: str


class ReservationSchema(Struct):
    """Capacity a cluster owes a deployment that has not bound a device yet.

    Derived on read and stored nowhere. It is the difference between what a
    deployment declares (this cluster's share, times the workload's device shape)
    and what ingestion has confirmed the cluster actually bound, so it appears the
    moment placement chooses a cluster and reaches zero on its own as the
    allocations arrive.

    Not an `AllocationSchema` and not on that route. An
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
