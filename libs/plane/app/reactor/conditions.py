"""Level-triggered evaluation: what is wrong right now, recomputed from scratch.

Four queries, one per condition, each returning the full current set. Recomputing
rather than tracking transitions is the design decision that matters here — it
means the reactor has no memory to lose, a restart mid-outage produces the same
answer as one that ran throughout, and "how long has this been true" is a fact
about the alert row (`first_seen_at`) rather than something this module has to
persist.

Every condition here answers a question `docs/plans/01` left to a human reading a
dashboard, and the four were chosen from one incident: a spot node went away, the
deployment on it stopped serving, and the plane stayed green for seventeen hours
because a cluster with nothing on it looks exactly like a cluster with nothing
wrong with it.
"""

import datetime as dt

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import ClusterSyncState
from app.db.models import DeploymentStatus
from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload

from . import rules
from . import tuning
from .rules import AlertIntent


LIVE_PHASES = ("active", "draining")
"""Phases a cluster is expected to be carrying work in.

`pending` is excluded because a cluster that has been enrolled and not yet
connected is not an incident, it is a Tuesday; `disabled` because someone turned
it off on purpose. Alerting on either is how an alerting system gets muted.
"""


def _tuned(pairs) -> list[AlertIntent]:
    """Applies each intent's labels and drops the ones they mute."""
    return [tuned for intent, labels in pairs if (tuned := tuning.apply(intent, labels)) is not None]


async def unreachable_clusters(db: AsyncSession) -> list[AlertIntent]:
    """Clusters that should be talking to us and are not.

    The outer join is load-bearing: a cluster whose agent has never connected has
    no `ClusterSyncState` row at all, and an inner join would quietly exclude the
    one case where nothing has ever worked.
    """
    rows = await db.execute(
        select(Cluster, ClusterSyncState)
        .outerjoin(ClusterSyncState, ClusterSyncState.cluster_id == Cluster.id)
        .where(Cluster.phase.in_(LIVE_PHASES))
    )
    intents = []
    for cluster, state in rows.all():
        if state is not None and state.reachable:
            continue
        last_seen = state.last_seen_at.isoformat() if state and state.last_seen_at else None
        intent = rules.cluster_unreachable(cluster.id, cluster.uid, cluster.name, last_seen)
        tuned = tuning.apply(intent, cluster.labels)
        if tuned is not None:
            intents.append(tuned)
    return intents


async def empty_clusters(db: AsyncSession) -> list[AlertIntent]:
    """Connected clusters reporting no devices — the spot-node case.

    Scoped to reachable clusters on purpose. An unreachable cluster also has no
    fresh inventory, and reporting both would mean two alerts for one problem with
    the less useful of them naming the wrong cause.
    """
    nodes = select(Node.cluster_id, func.count().label("nodes")).group_by(Node.cluster_id).subquery()
    devices = select(Device.cluster_id, func.count().label("devices")).group_by(Device.cluster_id).subquery()
    rows = await db.execute(
        select(Cluster, func.coalesce(nodes.c.nodes, 0), func.coalesce(devices.c.devices, 0))
        .join(ClusterSyncState, ClusterSyncState.cluster_id == Cluster.id)
        .outerjoin(nodes, nodes.c.cluster_id == Cluster.id)
        .outerjoin(devices, devices.c.cluster_id == Cluster.id)
        .where(Cluster.phase == "active", ClusterSyncState.reachable.is_(True))
    )
    return _tuned(
        (rules.cluster_no_capacity(cluster.id, cluster.uid, cluster.name, node_count, device_count), cluster.labels)
        for cluster, node_count, device_count in rows.all()
        if device_count == 0
    )


async def behind_deployments(db: AsyncSession) -> list[AlertIntent]:
    """Enabled deployments whose cluster reports fewer ready replicas than we asked for.

    Splits on whether a status row exists at all. Missing means the declared state
    has not reached the cluster — `deployment.unobserved`, an operator or sync
    problem. Present but short means it reached the cluster and the cluster cannot
    satisfy it — `deployment.behind`, a capacity problem. Reporting both as one
    alert would send people to the wrong half of the system.

    One alert per deployment, never per workload. A workload spread over two
    clusters that is short in one of them is short in that one cluster, and
    rolling the two into a single alert would name no cluster to go and look at.
    The counts compared here are the deployment's share, not the workload total.
    """
    rows = await db.execute(
        select(Deployment, DeploymentStatus, Cluster.name, Workload.labels)
        .outerjoin(DeploymentStatus, DeploymentStatus.deployment_id == Deployment.id)
        .join(Cluster, Cluster.id == Deployment.cluster_id)
        .join(Workload, Workload.id == Deployment.workload_id)
        .where(Deployment.enabled.is_(True), Deployment.desired_replicas > 0)
    )
    intents = []
    for deployment, status, cluster_name, labels in rows.all():
        if status is None:
            intent = rules.deployment_unobserved(
                deployment.cluster_id, cluster_name, deployment.uid, deployment.name, deployment.desired_replicas
            )
        elif status.ready_replicas < deployment.desired_replicas:
            intent = rules.deployment_behind(
                deployment.cluster_id,
                cluster_name,
                deployment.uid,
                deployment.name,
                status.ready_replicas,
                deployment.desired_replicas,
            )
        else:
            continue
        tuned = tuning.apply(intent, labels)
        if tuned is not None:
            intents.append(tuned)
    return intents


async def stalled_workloads(db: AsyncSession) -> list[AlertIntent]:
    """Sightings the cluster says are running nothing, imported or not.

    Only zero-ready, not merely-behind: a sighting is not something the plane
    declared, so "fewer than we wanted" is not a sentence this can finish. Zero
    ready against a non-zero desired needs no declared intent to be wrong.

    Workloads explicitly dismissed are skipped — a decision to stop showing
    something is a decision to stop being told about it, and an alerting system
    that ignores that is one people learn to ignore back.
    """
    dismissed = select(WorkloadAdoption.workload_uid).where(
        WorkloadAdoption.cluster_id == DiscoveredWorkload.cluster_id,
        WorkloadAdoption.workload_uid == DiscoveredWorkload.uid,
        WorkloadAdoption.decision == "ignored",
    )
    rows = await db.execute(
        select(DiscoveredWorkload, Cluster.name, Deployment.uid)
        .join(Cluster, Cluster.id == DiscoveredWorkload.cluster_id)
        .outerjoin(Deployment, Deployment.adopted_workload_uid == DiscoveredWorkload.uid)
        .where(
            DiscoveredWorkload.replicas_desired > 0,
            DiscoveredWorkload.replicas_ready == 0,
            ~dismissed.exists(),
        )
    )
    return _tuned(
        (
            rules.workload_stalled(
                workload.cluster_id,
                cluster_name,
                workload.uid,
                f"{workload.namespace}/{workload.name}",
                workload.replicas_ready,
                workload.replicas_desired,
                promoted=deployment_uid is not None,
            ),
            workload.labels,
        )
        for workload, cluster_name, deployment_uid in rows.all()
    )


async def evaluate(db: AsyncSession) -> list[AlertIntent]:
    """Every condition currently true, across the whole fleet.

    The caller treats this as authoritative and resolves any condition-sourced
    alert whose key is absent, so a query that fails must raise rather than
    return a short list — a partial answer here reads as "everything recovered".
    """
    intents: list[AlertIntent] = []
    intents += await unreachable_clusters(db)
    intents += await empty_clusters(db)
    intents += await behind_deployments(db)
    intents += await stalled_workloads(db)
    return intents


def overdue(first_seen: dt.datetime, now: dt.datetime, for_seconds: int) -> bool:
    """Whether a pending alert has been true for long enough to fire."""
    return (now - first_seen).total_seconds() >= for_seconds
