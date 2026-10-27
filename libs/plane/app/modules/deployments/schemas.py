import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class DeploymentReplicaRollupSchema(Struct):
    """Observed replicas, counted, not listed.

    A rollup because a deployment with two hundred replicas still answers this in
    one row.
    """

    total: int
    ready: int
    running: int
    pending: int
    failed: int
    at_current_generation: int


class DeploymentRoleObservedSchema(Struct):
    """One role's own share of a deployment's rollout, as its cluster counted it.

    `desired` is what that cluster rendered for the role and `ready` is what came
    up, both from one frame, which is what keeps the pair internally consistent
    in a way a join against `demand_replica` would not be.
    """

    role: str
    desired: int
    ready: int


class DeploymentStatusSchema(Struct):
    """What the cluster reports the deployment is doing, and why.

    `phase` is where it is; `reason` and `message` are what is holding it there.
    Most of a deployment's life before it serves is spent waiting on an ordering
    (the weights have to be resident, a node has to advertise enough accelerators,
    an image has to pull), and every one of those is an expected state
    instead of an error. They are kept apart from `last_error` for that reason:
    an error is a thing that went wrong and should be read as one, and folding a
    normal wait into it makes both unreadable.

    `reason` is a closed set the operator owns (`MESSAGES.md`) and this schema
    does not narrow it to a `Literal`. A cluster runs its own build
    and may report a sub-state this plane has never heard of; showing it beats
    rejecting the frame that carries it.

    Absent entirely when no cluster has reported yet, which is not the same as a
    deployment that is doing nothing.
    """

    observed_generation: int
    phase: str
    reason: str | None
    message: str | None
    desired_replicas: int
    ready_replicas: int
    updated_replicas: int
    unavailable_replicas: int
    last_error_code: str | None
    last_error_message: str | None
    last_error_at: dt.datetime | None
    observed_at: dt.datetime | None
    roles: list[DeploymentRoleObservedSchema] | None = None
    """The four scalars above split by role, and absent unless the deployment has roles.

    Additive by design: the scalars stay the group rollup and are not replaced, so a
    client that ignores this field reads the same correct totals it always did. What it
    cannot read is which role is short, which for a 1P:2D group is the only thing worth
    watching during a rollout: two ready decode workers and no prefill is `2 of 3`, a
    shortfall that reads as degraded and resolving, where nothing is serving at all.

    Stored on the observed row, not derived per read. The ready half could be
    counted over the replicas; the desired half could not, because how many of a role a
    cluster was asked for exists only in the frame that reported it.

    `None` instead of `[]` for an aggregated deployment. A single-entry array would make
    every deployment look like one with a declared ratio, and an empty one would claim the
    deployment has no roles instead of that the question does not apply.
    """


class DeploymentSchema(Struct):
    """One workload's share on one cluster: declared spec, admission, observed replicas.

    `min_replicas` / `max_replicas` / `desired_replicas` are THIS CLUSTER'S SHARE of
    the workload's total, never the total. Summing a share against a total, or
    dividing one out of the other, is always a bug.

    The serving configuration, adapters, overrides and labels are not here: they are
    workload facts, read through `workload_id`. Admission is here, because it is per-cluster: a workload can
    be running in one region and queued on quota in another.

    `adopted_workload_id` is the *discovered* workload this deployment was promoted from,
    and has nothing to do with `workload_id`. `authority` is who owns it: `observed`
    means the plane records and verifies and writes nothing to the cluster, `managed`
    means the plane renders and converges (ADR 0009).
    """

    id: str
    name: str
    workload_id: str
    workload_name: str
    cluster_id: str
    min_replicas: int
    max_replicas: int
    desired_replicas: int
    admission_phase: str
    admission_reason: str | None
    queued_at: dt.datetime | None
    admitted_at: dt.datetime | None
    placed_at: dt.datetime | None
    generation: int
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    adopted_workload_id: str | None
    adopted_fingerprint: str | None
    authority: str
    replicas: DeploymentReplicaRollupSchema
    status: DeploymentStatusSchema | None


class DeploymentUpdateRequest(Struct):
    """A partial update of one cluster's copy. An absent field is untouched.

    Everything a workload declares is absent: it is declared once, globally, and a
    per-cluster override of it would be a second answer to a question that already has
    one. Scale is absent because shares are derived by placement from the workload
    total, not asserted per cluster.
    """

    enabled: bool | UnsetType = UNSET


class DeploymentAuthorityRequest(Struct):
    """Take management of a deployment, or give it back.

    `fingerprint` is required only when taking management of an adopted workload,
    and it is the same gate promotion uses: the caller sends back the digest they
    were shown, and a workload that moved since refuses. Going to `managed` is the
    direction that can cost a restart, so it is the direction that has to prove the
    caller was looking at what is actually running.

    Releasing to `observed` needs nothing. The plane letting go never writes to a
    cluster, so there is no gap to fail closed on.
    """

    authority: str
    fingerprint: str | None = None
