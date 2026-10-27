"""How a workload and its history read back: derived phase, revision stamp, rollups."""

from __future__ import annotations

from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import DeploymentStatus
from app.db.models import WorkloadRevision
from app.modules.deployments.schemas import DeploymentStatusSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema
from app.modules.deployments.services import observed_roles

from .schemas import WorkloadRevisionSchema


UNITS = {"service": "replicas", "gang": "ranks", "task": "completions"}
"""What the three scale numbers count, named by kind.

The numbers are the same integers whatever the kind, and they mean three
different things; a console that renders `8` without saying whether that is eight
replicas or eight ranks is unreadable, so the unit travels with them.
"""


PHASE_ORDER = ("pending", "admitted", "placed", "running", "succeeded")
"""Admission phases from least to most advanced, for the workload rollup.

`failed` is intentionally absent: it is not a point on this line. One failed
deployment makes the workload failed regardless of how far the others got, which
is a different rule from "the least advanced wins" and is applied before it.
"""


EMPTY = DeploymentReplicaRollupSchema(
    total=0,
    ready=0,
    running=0,
    pending=0,
    failed=0,
    at_current_generation=0,
)


def derive_phase(deployments: list[Deployment]) -> str:
    """What a workload's phase is, given what became of it in each cluster.

    Derived and never stored. A workload that is running in one region and queued
    on quota in another has no single stored phase that is true, and inventing one
    would make the two indistinguishable from a workload that is halfway
    everywhere.

    A workload with no deployments at all is `pending`: placement either has not
    run yet or found nowhere to put it, and both are states it may still leave.
    """
    if not deployments:
        return "pending"
    phases = [d.admission_phase for d in deployments]
    if "failed" in phases:
        return "failed"
    ranked = [PHASE_ORDER.index(p) for p in phases if p in PHASE_ORDER]
    if not ranked:
        return "pending"
    return PHASE_ORDER[min(ranked)]


def _revision(workload: Workload, deployments: list[Deployment]) -> str:
    """The value `If-Match` is checked against, across the workload and its expansion.

    A workload is one row plus N, and a scale that only moved a share touches a
    deployment and leaves `demand_workload.updated_at` where it was. Deriving the
    revision from the workload alone would let that write go unnoticed by the next
    conditional update, so the latest stamp anywhere in the object wins.
    """
    stamps = [workload.updated_at or workload.created_at]
    stamps.extend(d.updated_at or d.created_at for d in deployments)
    return str(int(max(stamps).timestamp() * 1_000_000))


def _revision_view(row: WorkloadRevision) -> WorkloadRevisionSchema:
    """One revision as a read returns it.

    `serving` is handed back as the object it was stored as, not re-typed through
    `WorkloadServingUpdate`. A snapshot is a record of what was asked for at the time, and
    decoding it through today's struct would re-default a field added since and reject one
    removed since, which would make the oldest revisions, the ones most worth having, the
    first to become unreadable.
    """
    return WorkloadRevisionSchema(
        id=row.uid,
        number=row.number,
        serving_digest=row.serving_digest,
        artifact=row.artifact_uid,
        reason=row.reason,
        note=row.note,
        created_at=row.created_at,
        serving=row.snapshot,
    )


def _status(row: DeploymentStatus | None) -> DeploymentStatusSchema | None:
    if row is None:
        return None
    return DeploymentStatusSchema(
        observed_generation=row.observed_generation,
        phase=row.phase,
        reason=row.reason,
        message=row.message,
        desired_replicas=row.desired_replicas,
        ready_replicas=row.ready_replicas,
        updated_replicas=row.updated_replicas,
        unavailable_replicas=row.unavailable_replicas,
        roles=observed_roles(row.roles),
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        last_error_at=row.last_error_at,
        observed_at=row.observed_at,
    )


def _rollup(row: dict | None) -> DeploymentReplicaRollupSchema:
    if not row:
        return EMPTY
    return DeploymentReplicaRollupSchema(
        total=row.get("total", 0),
        ready=row.get("ready", 0),
        running=row.get("running", 0),
        pending=row.get("pending", 0),
        failed=row.get("failed", 0),
        at_current_generation=row.get("at_current_generation", 0),
    )
