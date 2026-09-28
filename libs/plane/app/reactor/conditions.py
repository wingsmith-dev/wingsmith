"""Level-triggered evaluation: what is wrong right now, recomputed from scratch.

Four queries, one per condition, each returning the full current set. Recomputing
rather than tracking transitions is the design decision that matters here — it
means the reactor has no memory to lose, a restart mid-outage produces the same
answer as one that ran throughout, and "how long has this been true" is a fact
about the alert row (`first_seen_at`) rather than something this module has to
persist.

Every condition here answers a question the founding object model left to a human reading a
dashboard, and the four were chosen from one incident: a spot node went away, the
deployment on it stopped serving, and the plane stayed green for seventeen hours
because a cluster with nothing on it looks exactly like a cluster with nothing
wrong with it.
"""

import datetime as dt

from logging import getLogger

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import AlertThreshold
from app.db.models import ClusterSyncState
from app.db.models import DeploymentStatus
from app.db.models import WorkloadAdoption
from app.db.models import DiscoveredWorkload

from . import rules
from . import tuning
from .rules import Threshold
from .rules import AlertIntent
from .rules import MetricSubject
from .measures import MetricQuery
from .measures import MetricReader
from .measures import ServiceMetricReader


logger = getLogger(__name__)


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


async def declared_thresholds(db: AsyncSession) -> list[Threshold]:
    """Every enabled threshold naming a rule this reactor knows how to evaluate.

    Rows for an unknown rule are left where they are rather than raising. A plane
    that has been rolled back sees rows written against a newer catalogue, and the
    right response to one is to evaluate the rules it does have — the write path
    is where a rule name is checked, and it rejects there.

    `value` is `Numeric` and arrives as `Decimal`; it is converted here, at the
    edge, so that nothing downstream has to know that mixing a `Decimal` with a
    float is an error in Python and a rounding decision in Postgres.

    Read inside a savepoint, and a table this cannot read is not fatal. A tick is
    one transaction — every condition, every event it drains and every notified
    marker it moves — and in Postgres one failed statement aborts all of it. A
    plane whose migration has not been applied yet would therefore lose its entire
    alerting pass because of the least important query in it, which is exactly
    backwards: the cluster that went away still needs someone told about it.
    """
    try:
        async with db.begin_nested():
            rows = await db.execute(
                select(AlertThreshold).where(
                    AlertThreshold.enabled.is_(True),
                    AlertThreshold.rule.in_(tuple(rules.METRIC_RULES)),
                )
            )
            declared = rows.scalars().all()
    except ProgrammingError:
        logger.warning("reactor.thresholds.unreadable", exc_info=True)
        return []
    return [
        Threshold(
            rule=row.rule,
            subject_type=row.subject_type,
            value=float(row.value),
            subject_uid=row.subject_uid,
            dimensions=dict(row.dimensions or {}),
            objective=row.objective,
            lookback=row.lookback,
            for_seconds=row.for_seconds,
            severity=row.severity,
            reason=row.reason,
        )
        for row in declared
    ]


async def deployment_subjects(db: AsyncSession) -> list[tuple[MetricSubject, dict]]:
    """Deployments a metric rule can be asked about, with the labels that tune them.

    Enabled and wanting replicas, for the same reason `behind_deployments` scopes
    that way: a deployment scaled to zero is serving nothing on purpose, and every
    serving metric about it is legitimately empty.

    The labels are the workload's, which is where `tuning` says the knob lives.
    """
    rows = await db.execute(
        select(Deployment.uid, Deployment.name, Deployment.cluster_id, Cluster.name, Workload.labels)
        .join(Cluster, Cluster.id == Deployment.cluster_id)
        .join(Workload, Workload.id == Deployment.workload_id)
        .where(Deployment.enabled.is_(True), Deployment.desired_replicas > 0)
    )
    return [
        (MetricSubject(uid=uid, name=name, cluster_id=cluster_id, cluster_name=cluster_name), labels)
        for uid, name, cluster_id, cluster_name, labels in rows.all()
    ]


async def node_subjects(db: AsyncSession) -> list[tuple[MetricSubject, dict]]:
    """Nodes in clusters we expect to be carrying work, with their own labels."""
    rows = await db.execute(
        select(Node.uid, Node.name, Node.cluster_id, Cluster.name, Node.labels)
        .join(Cluster, Cluster.id == Node.cluster_id)
        .where(Cluster.phase.in_(LIVE_PHASES))
    )
    return [
        (MetricSubject(uid=uid, name=name, cluster_id=cluster_id, cluster_name=cluster_name), labels)
        for uid, name, cluster_id, cluster_name, labels in rows.all()
    ]


SUBJECTS = {"deployment": deployment_subjects, "node": node_subjects}
"""How each subject type in `rules.METRIC_SUBJECTS` is enumerated."""


async def metric_thresholds(db: AsyncSession, reader: MetricReader) -> list[AlertIntent]:
    """Conditions from operator-declared numbers, one measurement at a time.

    The threshold table is the work list. Nothing is measured for a rule nobody
    declared a number for, so the cost of this pass is proportional to the
    commitments made rather than to the size of the fleet — which is the argument
    for a table of thresholds over a table of rules.

    Reading and judging are separate on purpose. This function decides which
    questions to ask and `reader` answers them; `rules.metric_breach` then decides
    what the answer means and has never heard of a metrics backend. A reading with
    no value produces no alert and no resolution either way — an unmeasurable
    subject is not a healthy one, and the pass says nothing about it rather than
    guessing in either direction.
    """
    declared = await declared_thresholds(db)
    if not declared:
        return []
    intents: list[AlertIntent] = []
    for subject_type in sorted({threshold.subject_type for threshold in declared}):
        enumerate_subjects = SUBJECTS.get(subject_type)
        if enumerate_subjects is None:
            logger.warning("reactor.thresholds.unknown_subject_type type=%s", subject_type)
            continue
        scoped = [t for t in declared if rules.METRIC_RULES[t.rule].subject_type == subject_type]
        wanted = sorted({t.rule for t in scoped})
        if not wanted:
            continue
        for subject, labels in await enumerate_subjects(db):
            for rule in wanted:
                threshold = rules.resolve_threshold(scoped, rule, subject.uid)
                if threshold is None:
                    continue
                definition = rules.METRIC_RULES[rule]
                reading = await reader.read(
                    MetricQuery(
                        metric=definition.metric,
                        subject_type=subject_type,
                        subject_uid=subject.uid,
                        lookback=threshold.lookback or definition.lookback,
                        dimensions=threshold.dimensions,
                    )
                )
                intent = rules.metric_breach(subject, threshold, reading.value)
                if intent is None:
                    continue
                tuned = tuning.apply(intent, labels)
                if tuned is not None:
                    intents.append(tuned)
    return intents


async def evaluate(db: AsyncSession, reader: MetricReader | None = None) -> list[AlertIntent]:
    """Every condition currently true, across the whole fleet.

    The caller treats this as authoritative and resolves any condition-sourced
    alert whose key is absent, so a query that fails must raise rather than
    return a short list — a partial answer here reads as "everything recovered".

    That is also why the metric pass is last and why it withholds rather than
    reports zero: a threshold whose measurement could not be taken contributes no
    key, which resolves its alert. Adding a metrics store to the fleet must not be
    a way to make an unrelated condition disappear, so a reader that cannot answer
    leaves the rest of the set untouched — but a store that is down does close the
    alerts it opened, which is the honest reading of "we can no longer see this".
    """
    intents: list[AlertIntent] = []
    intents += await unreachable_clusters(db)
    intents += await empty_clusters(db)
    intents += await behind_deployments(db)
    intents += await stalled_workloads(db)
    intents += await metric_thresholds(db, reader or ServiceMetricReader(db))
    return intents


def overdue(first_seen: dt.datetime, now: dt.datetime, for_seconds: int) -> bool:
    """Whether a pending alert has been true for long enough to fire."""
    return (now - first_seen).total_seconds() >= for_seconds
