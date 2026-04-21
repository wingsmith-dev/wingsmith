import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType
from app.modules.deployments.schemas import DeploymentStatusSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema


class WorkloadDeploymentSchema(Struct):
    """One cluster's share of a workload, as seen from the workload.

    Enough to render the per-cluster table and link to the deployment; the full
    per-cluster object is `GET /v1/deployments/{ref}`.
    """

    id: str
    name: str
    cluster_id: str
    cluster_name: str
    region: str | None
    min_replicas: int
    max_replicas: int
    desired_replicas: int
    admission_phase: str
    admission_reason: str | None
    generation: int
    enabled: bool
    authority: str
    queued_at: dt.datetime | None
    admitted_at: dt.datetime | None
    placed_at: dt.datetime | None
    replicas: DeploymentReplicaRollupSchema
    status: DeploymentStatusSchema | None


class WorkloadScaleSchema(Struct):
    """Declared totals, and what the shares actually add up to.

    `unit` is named by kind — `replicas` for service, `ranks` for gang, `completions`
    for task — because the three numbers count different things and a bare integer in
    a UI is unreadable without it. `placed` is the sum of the deployments' shares and
    `ready` the sum of their observed ready counts; they are derived here and are the
    only place summing is legal.
    """

    unit: str
    min: int
    max: int
    desired: int
    placed: int
    ready: int


class WorkloadSchema(Struct):
    """A workload: what was asked for, and where it ended up.

    `deployments` is the per-cluster expansion, one row per cluster placement chose.
    A `gang` workload always has exactly one; splitting a gang across clusters is
    rejected at placement, not ranked down.

    `phase` is derived from the deployments and is not stored: `running` when every
    deployment is running, `failed` if any failed, otherwise the least advanced
    phase among them. Nothing writes it.
    """

    id: str
    name: str
    kind: str
    phase: str
    project_id: str
    priority_class: str
    preemptible: bool
    paused: bool
    gang_size: int
    recipe_id: str
    adapter_artifact_ids: list[str]
    overrides: dict
    labels: dict
    requires_single_topology_domain: bool | None
    topology_domain_kind: str | None
    strategy: str
    cluster_floors: dict
    max_clusters: int | None
    pinned_cluster_id: str | None
    scale: WorkloadScaleSchema
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    deadline_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    deployments: list[WorkloadDeploymentSchema]
    recipe_name: str | None
    artifact_tag: str | None


class WorkloadPlacementNodeSchema(Struct):
    """One node a workload's live devices currently sit on, with its occupancy.

    `device_count` is how many of the node's devices this workload holds right
    now — the node's slot occupancy from the workload's point of view — derived
    from the live allocation rows rather than declared anywhere.
    """

    node: str
    cluster: str
    device_count: int


class WorkloadReplicaPlacementSchema(Struct):
    """Where one live allocation of a workload landed, replica by replica.

    `replica` is null for an allocation the cluster has not yet tied to a replica
    — a held allocation is a commitment of devices that may precede the process
    that runs on them. `gang_complete` says whether the allocation currently holds
    every device it was granted, so a partially satisfied gang is visible here.
    """

    allocation: str
    replica: str | None
    cluster: str
    nodes: list[str]
    devices: list[str]
    device_count: int
    gang_complete: bool


class WorkloadPlacementSchema(Struct):
    """Where a workload physically is, derived from its live allocations.

    Read-only and computed at read time from observed allocation, device and node
    rows scoped to the workload. Only membership and placement facts appear —
    no serving telemetry. `node_count` and `gpu_count` are the distinct live nodes
    and devices the workload holds across every cluster it landed on.
    """

    workload: str
    node_count: int
    gpu_count: int
    nodes: list[WorkloadPlacementNodeSchema]
    replicas: list[WorkloadReplicaPlacementSchema]


class WorkloadCreateRequest(Struct):
    """What it takes to run something, without saying where.

    `cluster` is optional, and an absent one is the normal case: placement picks,
    from the recipe's device shape, the project's quota, the policies attached to
    the workload, and what the clusters currently report free. Naming one is an
    override — a deliberate escape from the scheduler rather than the normal path —
    and it still has to exist, but it is not checked for capacity. An operator who
    names a cluster has said something placement is not entitled to overrule; a
    named cluster also forces a single deployment.

    `min_scale` / `max_scale` / `desired_scale` are TOTALS across every cluster this
    workload may use. They are counted in replicas for `service`, ranks for `gang`,
    completions for `task`.

    `priority_class` is optional rather than absent: every workload carries one, but
    the project's default answers for most callers and making them repeat it is how
    defaults drift.
    """

    recipe: str
    name: str
    project: str
    cluster: str | None = None
    priority_class: str | None = None
    kind: str = "service"
    min_scale: int = 1
    max_scale: int = 1
    desired_scale: int = 1
    strategy: str = "pack"
    cluster_floors: dict = {}
    max_clusters: int | None = None
    enabled: bool = True
    preemptible: bool = False
    gang_size: int = 1
    deadline_at: dt.datetime | None = None
    adapter_artifacts: list[str] = []
    overrides: dict = {}
    labels: dict = {}
    requires_single_topology_domain: bool | None = None
    topology_domain_kind: str | None = None


class WorkloadUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `recipe` and `cluster` are absent by design: a deployment is recipe x cluster, so
    changing either is a new deployment — which is cheap, because deployments are
    disposable and the endpoint in front of them is the stable name. Scale is absent
    too: changing it is long-running and can sit pending on quota forever, so it is
    the `workload.scale` operation and not a field write (rule 1).
    """

    name: str | UnsetType = UNSET
    adapter_artifacts: list[str] | UnsetType = UNSET
    overrides: dict | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    priority_class: str | UnsetType = UNSET
    preemptible: bool | UnsetType = UNSET
    deadline_at: dt.datetime | None | UnsetType = UNSET
    strategy: str | UnsetType = UNSET
    cluster_floors: dict | UnsetType = UNSET
    max_clusters: int | None | UnsetType = UNSET
    requires_single_topology_domain: bool | None | UnsetType = UNSET
    topology_domain_kind: str | None | UnsetType = UNSET
