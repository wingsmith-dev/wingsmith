import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class DeploymentWorkloadSchema(Struct):
    """The supertype half: what is scheduled, at what priority, and how far it got.

    These fields are not copied onto the deployment. `type` discriminates
    `service | task | gang` and `phase` runs `pending → admitted → placed →
    running`, both on `Workload`, so preemption and quota stay one code path
    across serving and batch instead of three.
    """

    type: str
    phase: str
    reason: str | None
    project_id: str
    priority_class: str
    preemptible: bool
    gang_size: int
    queued_at: dt.datetime | None
    admitted_at: dt.datetime | None
    placed_at: dt.datetime | None
    started_at: dt.datetime | None
    deadline_at: dt.datetime | None


class DeploymentReplicaRollupSchema(Struct):
    """Observed replicas, counted rather than listed.

    A rollup because a deployment with two hundred replicas still answers this in
    one row; a client that wants the replicas asks `GET /v1/replicas?deployment=`,
    which is also the only module allowed to write them.
    """

    total: int
    ready: int
    running: int
    pending: int
    failed: int
    at_current_generation: int


class DeploymentSchema(Struct):
    """Declared spec, the workload it is a specialisation of, and observed replicas.

    Three parts kept apart on purpose: the spec is what we asked for, `workload`
    is the scheduling supertype, and `replicas` is what the cluster reports.
    """

    id: str
    name: str
    cluster_id: str
    recipe_id: str
    min_replicas: int
    max_replicas: int
    desired_replicas: int
    generation: int
    enabled: bool
    adapter_artifact_ids: list[str]
    overrides: dict
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    workload: DeploymentWorkloadSchema
    replicas: DeploymentReplicaRollupSchema


class DeploymentCreateRequest(Struct):
    """What it takes to place a recipe on a cluster.

    `priority_class` is optional rather than absent: every workload carries one,
    but the project's default answers for most callers and making them repeat it
    is how defaults drift.
    """

    cluster: str
    recipe: str
    name: str
    project: str
    priority_class: str | None = None
    type: str = "service"
    min_replicas: int = 1
    max_replicas: int = 1
    desired_replicas: int = 1
    enabled: bool = True
    preemptible: bool = False
    gang_size: int = 1
    deadline_at: dt.datetime | None = None
    adapter_artifacts: list[str] = []
    overrides: dict = {}


class DeploymentUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `cluster` and `recipe` are absent by design: a deployment is recipe × cluster,
    so changing either is a new deployment — which is cheap, because deployments
    are disposable and the endpoint in front of them is the stable name.
    `phase` is absent too; admission is decided by placement, not asserted by a
    client.
    """

    name: str | UnsetType = UNSET
    min_replicas: int | UnsetType = UNSET
    max_replicas: int | UnsetType = UNSET
    desired_replicas: int | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    adapter_artifacts: list[str] | UnsetType = UNSET
    overrides: dict | UnsetType = UNSET
    priority_class: str | UnsetType = UNSET
    preemptible: bool | UnsetType = UNSET
    deadline_at: dt.datetime | None | UnsetType = UNSET
