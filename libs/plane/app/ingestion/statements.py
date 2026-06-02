"""The statements ingestion writes, built and nothing else.

Kept apart from the sink so that every write this package can perform is one
pure function of a decoded payload, compilable and assertable without a
database. The two package rules — observed state only, no telemetry — are
enforced here, by which columns a builder is willing to name.
"""

from uuid import UUID
from typing import Any

import msgspec

from sqlalchemy import Delete
from sqlalchemy import Insert
from sqlalchemy import Select
from sqlalchemy import Update
from sqlalchemy import func
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy import literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.ids import NODE
from app.core.ids import EVENT
from app.core.ids import DEVICE
from app.core.ids import REPLICA
from app.core.ids import ALLOCATION
from app.core.ids import TOPOLOGY_DOMAIN
from app.core.ids import DISCOVERED_WORKLOAD
from app.core.ids import is_uid
from app.core.ids import new_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Event
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Replica
from app.db.models import Workload
from app.db.models import Component
from app.db.models import Operation
from app.db.models import Allocation
from app.db.models import CacheEntry
from app.db.models import Deployment
from app.db.models import BenchmarkRun
from app.db.models import TopologyDomain
from app.db.models import ComponentStatus
from app.db.models import AllocationDevice
from app.db.models import ClusterSyncState
from app.db.models import DeploymentStatus
from app.db.models import DiscoveredWorkload
from app.db.models import TopologyDomainNode
from app.hub.messages import NodeEvent
from app.hub.messages import DeviceEvent
from app.hub.messages import NodeObserved
from app.hub.messages import ReplicaEvent
from app.hub.messages import CacheObserved
from app.hub.messages import DeviceObserved
from app.hub.messages import ClusterObserved
from app.hub.messages import ReplicaObserved
from app.hub.messages import TopologyObserved
from app.hub.messages import BenchmarkObserved
from app.hub.messages import ComponentObserved
from app.hub.messages import OperationObserved
from app.hub.messages import AllocationObserved
from app.hub.messages import DeploymentObserved
from app.hub.messages import WorkloadDiscovered
from app.ingestion.time import parse_ts


NODE_CONSTRAINT = "uq_supply_node_cluster_name"
DEVICE_CONSTRAINT = "uq_supply_device_cluster_uuid"
TOPOLOGY_CONSTRAINT = "uq_supply_topology_domain_identity"
DOMAIN_NODE_CONSTRAINT = "uq_supply_topology_domain_node"
DISCOVERED_CONSTRAINT = "uq_demand_discovered_workload_identity"
REPLICA_CONSTRAINT = "uq_demand_replica_cluster_name"
CACHE_CONSTRAINT = "uq_ops_cache_entry_scope"


EVENT_SEVERITY = {
    "joined": "info",
    "left": "warning",
    "cordoned": "info",
    "uncordoned": "info",
    "degraded": "warning",
    "recovered": "info",
    "reset": "info",
    "fell_off_bus": "error",
    "started": "info",
    "ready": "info",
    "failed": "error",
    "oom": "error",
    "preempted": "warning",
}


class MalformedObservation(Exception):
    """A frame that can never be applied, however many times it is replayed.

    Distinct from an ordinary failure on purpose. The protocol has no negative
    acknowledgement, so the only way to refuse a frame is to withhold the ack and
    be sent it again — which is right for a transient failure and a wedged stream
    for a permanent one. See PROTOCOL.md 6b.
    """


def observed_uid(wire_id: str, prefix: str) -> str:
    """The public id an observed row carries.

    Minted by the cluster, not by the plane: an observed object needs a handle
    from the moment it is discovered, and later frames cross-reference it by that
    handle before the plane has necessarily seen the row it names (ADR 0002). The
    plane validates the shape and stores it verbatim — `uid` never appears in an
    update set, so a row rediscovered after a reconnect keeps the id it was first
    given.
    """
    if not is_uid(wire_id) or not wire_id.startswith(prefix):
        raise MalformedObservation(f"{wire_id!r} is not a {prefix} identifier")
    return wire_id


def cluster_ref_clause(ref: str):
    """Matches a cluster by whichever identity the caller happens to hold.

    An operator is configured by hand, long before anyone has read back the uid
    the plane minted for its cluster, so every lookup on the sync path accepts a
    name too — the same way the REST layer does. Both identities are immutable,
    and `is_uid` checks the body's shape as well as the prefix, so a name can
    never be mistaken for a uid.
    """
    return (Cluster.uid if is_uid(ref) else Cluster.name) == ref


def cluster_ref(ref: str):
    return select(Cluster.id).where(cluster_ref_clause(ref)).scalar_subquery()


def _node_ref(cluster_id: UUID, node_uid: str):
    return select(Node.id).where(Node.cluster_id == cluster_id, Node.uid == node_uid).scalar_subquery()


def _pool_ref(cluster_id: UUID, name: str):
    return select(Pool.id).where(Pool.cluster_id == cluster_id, Pool.name == name).scalar_subquery()


def _device_ref(cluster_id: UUID, device_uid: str):
    return select(Device.id).where(Device.cluster_id == cluster_id, Device.uid == device_uid).scalar_subquery()


def _device_class_ref(cluster_id: UUID, device_uids: list[str]):
    """The class of the devices an allocation holds, read back rather than restated.

    A gang is homogeneous by construction — the scheduler cannot commit an H100
    and an A100 to one replica — so the first device answers for all of them.
    """
    return (
        select(Device.device_class)
        .where(Device.cluster_id == cluster_id, Device.uid.in_(device_uids))
        .limit(1)
        .scalar_subquery()
    )


def _domain_ref(cluster_id: UUID, domain_uid: str):
    return (
        select(TopologyDomain.id)
        .where(TopologyDomain.cluster_id == cluster_id, TopologyDomain.uid == domain_uid)
        .scalar_subquery()
    )


def _upsert(model: Any, values: dict[str, Any], constraint: str, keep: set[str] | None = None) -> Insert:
    """An upsert whose update set is the insert values minus the identity columns.

    `uid`, `cluster_id`, and the natural key are what the row *is*; everything
    else is what was last observed about it. Excluding the first group from the
    update set is what makes a rediscovered row keep its identity.

    `keep` names further columns a rediscovery must not move — a first-sighting
    stamp is written once and is wrong the moment an upsert refreshes it.
    """
    frozen = {"uid", "cluster_id", "name", "device_uuid", "kind", "identifier", "namespace"} | (keep or set())
    statement = pg_insert(model).values(**values)
    return statement.on_conflict_do_update(
        constraint=constraint,
        set_={key: statement.excluded[key] for key in values if key not in frozen},
    )


def node_condition(payload: NodeObserved) -> str:
    if payload.membership != "joined":
        return payload.membership
    if not payload.ready:
        return "not_ready"
    if any(c.type != "ready" and c.status == "true" for c in payload.conditions):
        return "degraded"
    return "ready"


def upsert_node(cluster_id: UUID, payload: NodeObserved) -> Insert:
    """Writes one observed node.

    `runtime_version` is on the wire and deliberately not written: `Node` has no
    orchestrator- or runtime-specific version column, because the plane does not
    know a cluster is Kubernetes.

    The machine's shape — instance type, cloud, zone, region, cores, memory,
    architecture, os image — is written only when the frame carries it. An older
    operator omits these fields entirely, and an upsert that wrote them as nulls
    would erase what a newer one had already supplied every time an old agent
    reconnected.

    For the scalars an empty string and a zero stand in for absent, which costs
    nothing real: a node with no architecture and a node with zero cores are both
    nodes nobody has described yet. `taints` cannot borrow that trick — an empty
    taint list is a fact, and a node that has just had its last taint removed
    must be able to say so — so it defaults to None on the wire and is written
    only when the key was actually present.
    """
    values: dict[str, Any] = {
        "uid": observed_uid(payload.node_id, NODE),
        "cluster_id": cluster_id,
        "name": payload.name,
        "pool_id": _pool_ref(cluster_id, payload.pool) if payload.pool else None,
        "condition": node_condition(payload),
        "conditions": [msgspec.structs.asdict(c) for c in payload.conditions],
        "cordoned": payload.cordoned,
        "ready": payload.ready,
        "kernel_version": payload.kernel_version or None,
        "driver_version": payload.driver_version or None,
        "operator_version": payload.operator_version or None,
        "labels": payload.labels,
        "observed_at": parse_ts(payload.observed_at),
    }
    if payload.taints is not None:
        values["taints"] = [msgspec.structs.asdict(t) for t in payload.taints]
    optional = {
        "instance_type": payload.instance_type,
        "cloud": payload.cloud,
        "zone": payload.zone,
        "region": payload.region,
        "cpu_arch": payload.cpu_arch,
        "os_image": payload.os_image,
        "cpu_cores": payload.cpu_cores,
        "memory_mib": payload.memory_mib,
    }
    values.update({column: value for column, value in optional.items() if value})
    return _upsert(Node, values, NODE_CONSTRAINT)


def upsert_cluster_observed(cluster_id: UUID, payload: ClusterObserved) -> Insert:
    """Writes what a cluster reported about itself.

    Lands on the sync state and never on the registry row: this is observed, and
    `Cluster` has exactly one writer that is not ingestion. `region` is stored as
    `observed_region` beside the declared one rather than over it — a cluster
    enrolled as `eu-west-1` whose machines are all in `us-east-2` is a fact
    somebody needs to see, not a field to quietly correct.
    """
    return upsert_sync_state(
        cluster_id,
        {
            "distribution": payload.distribution or None,
            "cloud": payload.cloud or None,
            "observed_region": payload.region or None,
            "read_only": payload.read_only,
            "multinode": payload.multinode,
            "last_sync_at": parse_ts(payload.observed_at),
        },
        bump=True,
    )


def upsert_device(cluster_id: UUID, payload: DeviceObserved) -> Insert:
    """Writes one observed device.

    `memory_mib` is the device's capacity, which is state; how much of it is in
    use is a measurement and is not on the wire at all. Dropped as telemetry:
    `ecc.volatile_corrected` (a rate, not a state — corrected errors are counted
    by the metrics pipeline). The uncorrected ECC counters are kept because they
    are what makes a device unschedulable. `allocation` is not written here: a
    device's commitment is an `Allocation`, not a column.
    """
    domain = payload.topology_domain_id
    values: dict[str, Any] = {
        "uid": observed_uid(payload.device_id, DEVICE),
        "cluster_id": cluster_id,
        "node_id": _node_ref(cluster_id, payload.node_id),
        "topology_domain_id": _domain_ref(cluster_id, domain) if domain else None,
        "device_uuid": payload.uuid,
        "device_index": payload.index,
        "device_class": payload.device_class,
        "model_name": payload.model or None,
        "memory_mib": payload.memory_total_mb,
        "health": payload.health,
        "ecc_enabled": payload.ecc.mode_enabled,
        "ecc_volatile_errors": payload.ecc.volatile_uncorrected,
        "ecc_aggregate_errors": payload.ecc.aggregate_uncorrected,
        "throttled": payload.throttled,
        "throttle_reasons": payload.throttle_reasons,
        "mig_enabled": payload.mig.enabled,
        "mig_profile": payload.mig.profile,
        "observed_at": parse_ts(payload.observed_at),
    }
    return _upsert(Device, values, DEVICE_CONSTRAINT)


def upsert_discovered_workload(cluster_id: UUID, payload: WorkloadDiscovered) -> Insert:
    """Writes one sighting of a workload the plane did not declare.

    `first_seen_at` is in the insert values and in the frozen set, so it records
    when we first saw the workload and is never moved by a later observation —
    "running here since before we arrived" is the fact an operator adopting it
    wants, and an upsert that refreshed it on every tick would destroy it.

    Replica counts are the cluster's own accounting rather than a measurement,
    which is what keeps them on this side of the telemetry line.
    """
    observed = parse_ts(payload.observed_at)
    values: dict[str, Any] = {
        "uid": observed_uid(payload.workload_id, DISCOVERED_WORKLOAD),
        "cluster_id": cluster_id,
        "namespace": payload.namespace,
        "name": payload.name,
        "kind": payload.kind,
        "model": payload.model,
        "images": payload.images,
        "replicas_desired": payload.replicas_desired,
        "replicas_ready": payload.replicas_ready,
        "devices_per_replica": payload.devices_per_replica,
        "devices_total": payload.devices_total,
        "node_uids": payload.node_ids,
        "labels": payload.labels,
        "spec_fingerprint": payload.spec_fingerprint or None,
        "spec": msgspec.to_builtins(payload.spec) if payload.spec is not None else {},
        "first_seen_at": observed,
        "observed_at": observed,
    }
    return _upsert(DiscoveredWorkload, values, DISCOVERED_CONSTRAINT, keep={"first_seen_at"})


def upsert_deployment_status(cluster_id: UUID, deployment_id: UUID, payload: DeploymentObserved) -> Insert:
    """Writes the observed half of a deployment.

    Its own row rather than columns on `demand_deployment`, for the reason
    `ComponentStatus` is its own row: the declared half is written by the API and
    this half by the cluster, and one row with two writers is the difference rule
    1 exists to keep. `observed_generation` is what makes the counts readable —
    four ready at the previous generation and four ready at the current one are
    the same integers and different facts.
    """
    error = payload.last_error
    values: dict[str, Any] = {
        "deployment_id": deployment_id,
        "cluster_id": cluster_id,
        "observed_generation": payload.generation,
        "phase": payload.phase,
        "desired_replicas": payload.desired_replicas,
        "ready_replicas": payload.ready_replicas,
        "updated_replicas": payload.updated_replicas,
        "unavailable_replicas": payload.unavailable_replicas,
        "reason": payload.reason,
        "message": payload.message,
        "last_error_code": error.code if error else None,
        "last_error_message": error.message if error else None,
        "last_error_at": parse_ts(error.at) if error else None,
        "observed_at": parse_ts(payload.observed_at),
    }
    statement = pg_insert(DeploymentStatus).values(**values)
    return statement.on_conflict_do_update(
        index_elements=[DeploymentStatus.deployment_id],
        set_={key: statement.excluded[key] for key in values if key not in {"deployment_id", "cluster_id"}},
    )


ADMISSION_PHASE_FOR_OBSERVED = {
    "pending": "placed",
    "admitted": "placed",
    "placed": "placed",
    "degraded": "running",
    "running": "running",
    "failed": "failed",
}
"""What a cluster's observed phase means for that cluster's admission phase.

The two enums overlap in spelling and not in meaning, which is why the mapping is
written out rather than passed through. Admission is about how far through the
scheduler the work got; the observed phase is about what the cluster is doing
with it. So `degraded` maps to `running` — a deployment missing one of four
replicas is running, just badly, and admission has no opinion on health — while
an observed `pending` maps to `placed` rather than back to `pending`, because
placement already chose a cluster and waiting for weights does not un-choose it.

`terminating` is absent on purpose. A deployment being torn down is between
phases rather than in a new one, and the delete that follows is what ends it.
"""


def advance_admission_phase(deployment_id: UUID, payload: DeploymentObserved) -> Update | None:
    """Moves one deployment's admission phase to match what its cluster reports.

    The gap this closes: `admission_phase` was written once, as `placed`, when the
    deployment was created, and nothing ever wrote it again. Every deployment this
    plane created therefore read `placed` for its entire life — including while it
    was serving — and the only thing that ever reported `running` was a workload
    adopted from a cluster that was already running one.

    Admission is per-cluster, so this lands on the `Deployment` rather than on the
    workload above it. A workload fanned out across two regions can be running in
    one and queued on quota in the other, and there is no single phase that is true
    of both; writing the workload from here would let whichever cluster reported
    last stomp every other cluster's answer.

    Derived rather than latched, so a deployment whose replicas all die reads
    `placed` again instead of claiming a `running` that stopped being true.
    """
    phase = ADMISSION_PHASE_FOR_OBSERVED.get(payload.phase)
    if phase is None:
        return None
    values: dict[str, Any] = {"admission_phase": phase, "admission_reason": payload.reason}
    return update(Deployment).where(Deployment.id == deployment_id).values(**values)


def roll_up_workload_started(workload_id: UUID, payload: DeploymentObserved) -> Update:
    """Stamps the workload with the first moment any of its clusters ran.

    The rollup half of the statement above. `started_at` is global where the phase
    is not: a workload started when the earliest of its deployments started, and it
    is that earliest moment for the rest of its life. `coalesce` with `least` is
    what makes the write a `min()` rather than a race — frames from N clusters
    arrive in no particular order, and every one of them may only move the value
    backwards, never forwards.

    A workload that ran and stopped still ran, so nothing ever clears this.
    """
    moment = parse_ts(payload.observed_at)
    return (
        update(Workload)
        .where(Workload.id == workload_id)
        .values(started_at=func.least(func.coalesce(Workload.started_at, moment), moment))
    )


def upsert_replica(cluster_id: UUID, deployment_id: UUID, payload: ReplicaObserved) -> Insert:
    """Writes one observed replica.

    `name` is the cluster-local name and the natural key the upsert conflicts on;
    `uid` is the `rep_` handle other frames cross-reference it by, and is frozen
    so a replica re-reported after a reconnect keeps the identity it was first
    given. The devices it holds are not written here — a replica's commitment is
    an `Allocation`, which `allocation.observed` carries — and `engine` is not
    stored because it is the workload's, not the replica's; `engine_version` is,
    because that is the one that lags during a rollout.
    """
    values: dict[str, Any] = {
        "uid": observed_uid(payload.replica_id, REPLICA),
        "cluster_id": cluster_id,
        "deployment_id": deployment_id,
        "node_id": _node_ref(cluster_id, payload.node_id) if payload.node_id else None,
        "name": payload.name,
        "phase": payload.phase,
        "ready": payload.ready,
        "engine_version": payload.engine_version or None,
        "observed_generation": payload.observed_generation,
        "restart_count": payload.restart_count,
        "started_at": parse_ts(payload.started_at) if payload.started_at else None,
        "observed_at": parse_ts(payload.observed_at),
    }
    return _upsert(Replica, values, REPLICA_CONSTRAINT)


def upsert_allocation(
    cluster_id: UUID,
    workload_id: UUID,
    deployment_id: UUID,
    project_id: UUID,
    replica_id: UUID | None,
    payload: AllocationObserved,
) -> Insert:
    """Writes one observed allocation — the devices committed together to a replica.

    `device_class` is not on the wire and is read back from the devices named,
    because the class is a property of the accelerator and having the cluster
    restate it would create a second place for it to be wrong. `deployment_id` is
    recorded beside `workload_id` because devices are held per-cluster: a workload
    fanned out across regions holds accelerators in each of them, and preemption
    has to be able to name which cluster's copy it is taking devices from.
    `phase` is `held`:
    an allocation the cluster is still reporting is one it is still holding, and
    release is expressed by the allocation ceasing to be reported rather than by
    a phase the plane could assert on its own.
    """
    values: dict[str, Any] = {
        "uid": observed_uid(payload.allocation_id, ALLOCATION),
        "cluster_id": cluster_id,
        "workload_id": workload_id,
        "deployment_id": deployment_id,
        "replica_id": replica_id,
        "project_id": project_id,
        "device_class": _device_class_ref(cluster_id, payload.device_ids),
        "device_count": len(payload.device_ids),
        "phase": "held",
        "acquired_at": parse_ts(payload.granted_at),
        "observed_at": parse_ts(payload.observed_at),
    }
    statement = pg_insert(Allocation).values(**values)
    return statement.on_conflict_do_update(
        index_elements=[Allocation.uid],
        set_={key: statement.excluded[key] for key in values if key not in {"uid", "cluster_id"}},
    )


def attach_allocation_devices(allocation_id: UUID, cluster_id: UUID, payload: AllocationObserved) -> Insert:
    """Adds the devices an allocation names, leaving ones already recorded alone.

    `ON CONFLICT DO NOTHING` rather than an upsert: membership of an allocation is
    fixed at the moment it is granted, so a device already in it is not news, and
    re-inserting would move `released_at` on a row the release path owns.
    """
    rows = [
        {
            "allocation_id": allocation_id,
            "device_id": _device_ref(cluster_id, device_uid),
        }
        for device_uid in payload.device_ids
    ]
    statement = pg_insert(AllocationDevice).values(rows)
    return statement.on_conflict_do_nothing(constraint="uq_bind_allocation_device")


def release_allocation_devices(allocation_id: UUID, cluster_id: UUID, payload: AllocationObserved) -> Update:
    """Marks released every device this allocation holds that the cluster no longer names.

    Stamping `released_at` rather than deleting the row is what the partial unique
    index on live devices is for: history stays, and the accelerator becomes
    claimable again in the same statement.
    """
    return (
        update(AllocationDevice)
        .where(
            AllocationDevice.allocation_id == allocation_id,
            AllocationDevice.released_at.is_(None),
            ~AllocationDevice.device_id.in_(
                select(Device.id).where(Device.cluster_id == cluster_id, Device.uid.in_(payload.device_ids))
            ),
        )
        .values(released_at=parse_ts(payload.observed_at))
    )


def upsert_cache_entries(cluster_id: UUID, artifact_id: UUID, payload: CacheObserved) -> list[Insert]:
    """Writes artifact residency: one cluster-scoped row, plus one row per node.

    Both, not one or the other. The per-node rows are what a cache-aware router
    reads — it has to send a request to a node that actually holds the weights,
    and a node mid-fetch is not that node. The cluster-scoped row (`node_id` null,
    `tier` `cluster`) is what a shared tier's residency has to live on, because it
    is real and has no node to attribute it to.
    """
    statements = [
        _cache_entry(
            {
                "cluster_id": cluster_id,
                "node_id": None,
                "artifact_id": artifact_id,
                "tier": "cluster",
                "state": payload.state,
                "bytes_resident": payload.bytes_resident,
                "path": None,
                "observed_at": parse_ts(payload.observed_at),
                "last_verified_at": parse_ts(payload.last_accessed_at) if payload.last_accessed_at else None,
            }
        )
    ]
    statements.extend(
        _cache_entry(
            {
                "cluster_id": cluster_id,
                "node_id": _node_ref(cluster_id, node.node_id),
                "artifact_id": artifact_id,
                "tier": "node",
                "state": node.state,
                "bytes_resident": node.bytes_resident,
                "path": node.path,
                "observed_at": parse_ts(payload.observed_at),
                "last_verified_at": parse_ts(payload.last_accessed_at) if payload.last_accessed_at else None,
            }
        )
        for node in payload.nodes
    )
    return statements


def _cache_entry(values: dict[str, Any]) -> Insert:
    statement = pg_insert(CacheEntry).values(**values)
    return statement.on_conflict_do_update(
        constraint=CACHE_CONSTRAINT,
        set_={
            key: statement.excluded[key]
            for key in values
            if key not in {"cluster_id", "node_id", "artifact_id", "tier"}
        },
    )


def upsert_topology_domain(cluster_id: UUID, payload: TopologyObserved) -> Insert:
    """Writes one observed topology domain. Membership goes to the join table.

    The domain row says what the fabric is; `supply_topology_domain_node` says
    which machines it reaches. They are written in the same frame but not in the
    same statement, because a domain that spans four nodes is four rows and the
    domain itself is one.
    """
    values: dict[str, Any] = {
        "uid": observed_uid(payload.domain_id, TOPOLOGY_DOMAIN),
        "cluster_id": cluster_id,
        "kind": payload.kind,
        "identifier": payload.domain_id,
        "device_count": len(payload.device_ids),
        "spans_nodes": payload.spans_nodes,
        "observed_at": parse_ts(payload.observed_at),
    }
    return _upsert(TopologyDomain, values, TOPOLOGY_CONSTRAINT)


def replace_domain_nodes(cluster_id: UUID, payload: TopologyObserved) -> list[Delete | Insert]:
    """Makes the join table say exactly what this frame said, and nothing older.

    A delete of the members the frame no longer names, then an insert of the ones
    it does — last-writer-wins per cluster, the same rule the rest of ingestion
    follows. The insert is `ON CONFLICT DO NOTHING` rather than an upsert because
    membership carries nothing to update: the row either exists or it does not.
    Nodes the plane has not ingested yet contribute no row and reappear on the
    next observation of the same domain.
    """
    domain = _domain_ref(cluster_id, payload.domain_id)
    members = select(Node.id).where(Node.cluster_id == cluster_id, Node.uid.in_(payload.node_ids))
    statements: list[Delete | Insert] = [
        delete(TopologyDomainNode).where(
            TopologyDomainNode.domain_id == domain,
            TopologyDomainNode.node_id.notin_(members),
        )
    ]
    if payload.node_ids:
        insertion = pg_insert(TopologyDomainNode).from_select(
            ["domain_id", "node_id"],
            select(domain.label("domain_id"), Node.id.label("node_id")).where(
                Node.cluster_id == cluster_id, Node.uid.in_(payload.node_ids)
            ),
        )
        statements.append(insertion.on_conflict_do_nothing(constraint=DOMAIN_NODE_CONSTRAINT))
    return statements


def attach_domain_devices(cluster_id: UUID, payload: TopologyObserved) -> Update:
    """Points the devices a domain names at it, for devices whose own frame lags."""
    return (
        update(Device)
        .where(Device.cluster_id == cluster_id, Device.uid.in_(payload.device_ids))
        .values(topology_domain_id=_domain_ref(cluster_id, payload.domain_id))
    )


def upsert_component_status(cluster_id: UUID, payload: ComponentObserved) -> Insert:
    """Writes the observed half of a component.

    Keyed on the declared `Component` it belongs to: a status for a component
    nobody declared has nothing to be the observed half *of*, and the insert
    resolves to a null component and fails rather than inventing one.
    """
    component = (
        select(Component.id).where(Component.cluster_id == cluster_id, Component.name == payload.name).scalar_subquery()
    )
    values: dict[str, Any] = {
        "component_id": component,
        "cluster_id": cluster_id,
        "observed_version": payload.running_version or None,
        "rollout_phase": payload.phase,
        "healthy": payload.phase == "healthy",
        "message": payload.last_error.message if payload.last_error else None,
        "observed_at": parse_ts(payload.observed_at),
    }
    statement = pg_insert(ComponentStatus).values(**values)
    return statement.on_conflict_do_update(
        index_elements=[ComponentStatus.component_id],
        set_={key: statement.excluded[key] for key in values if key not in {"component_id", "cluster_id"}},
    )


def update_operation(cluster_id: UUID, payload: OperationObserved) -> Update:
    """Applies observed progress to an operation the plane declared.

    An `UPDATE` and never an upsert: an operation is imperative and requested by
    somebody, so ingestion may report on one but must never bring one into
    existence. `progress` is not written — there is no column for it, and a
    percentage between two phases is a measurement.
    """
    values: dict[str, Any] = {
        "phase": payload.phase,
        "result": payload.result,
        "error": payload.failure.message if payload.failure else None,
        "started_at": parse_ts(payload.started_at),
        "finished_at": parse_ts(payload.completed_at),
    }
    return (
        update(Operation)
        .where(Operation.uid == payload.operation_id, Operation.cluster_id == cluster_id)
        .values(**{key: value for key, value in values.items() if value is not None})
    )


def update_benchmark_run(cluster_id: UUID, payload: BenchmarkObserved) -> Update:
    """Applies a harness's report to the run the plane declared.

    An `UPDATE` for the same reason `update_operation` is one: a run is asked for
    by somebody, and ingestion reports on one rather than inventing one.

    `summary` is written through untouched. It is the harness's output shape and
    this seam has no opinion about it — parsing it here to fill normalised
    columns is exactly the thing this design defers until two harnesses have been
    read in anger.

    Unlike the operation update, a null is written where the payload carries one
    for `summary`, `error` and `artifacts_uri`: a run that failed reports no
    summary, and the filter that protects `started_at` from being cleared by a
    progress frame would otherwise leave a stale summary from a previous attempt
    beside a failed phase.
    """
    values: dict[str, Any] = {
        "phase": payload.phase,
        "harness_version": payload.harness_version,
        "started_at": parse_ts(payload.started_at),
        "finished_at": parse_ts(payload.completed_at),
    }
    always: dict[str, Any] = {
        "summary": payload.summary,
        "artifacts_uri": payload.artifacts_uri,
        "error": payload.error,
    }
    return (
        update(BenchmarkRun)
        .where(BenchmarkRun.uid == payload.benchmark_id, BenchmarkRun.cluster_id == cluster_id)
        .values(**{key: value for key, value in values.items() if value is not None}, **always)
    )


def _insert_event(
    cluster_id: UUID,
    type_name: str,
    severity: str,
    subject_type: str,
    subject_id: Any,
    message: str,
    details: dict[str, str],
    occurred_at: Any,
) -> Insert:
    """An append-only event insert, guarded against replaying the same event twice.

    The guard is `ON CONFLICT DO NOTHING` against `uq_ops_event_occurrence`,
    which is the unique index over the tuple that identifies one occurrence —
    cluster, type, subject kind, instant, and the message. A `NOT EXISTS` read
    first would still let two sessions replaying the same frame both see nothing
    and both insert; the index is the only thing that decides between them.
    """
    columns = [
        "uid",
        "cluster_id",
        "type",
        "severity",
        "subject_type",
        "subject_id",
        "message",
        "details",
        "occurred_at",
    ]
    values = select(
        literal(new_uid(EVENT)).label("uid"),
        literal(cluster_id).label("cluster_id"),
        literal(type_name).label("type"),
        literal(severity).label("severity"),
        literal(subject_type).label("subject_type"),
        subject_id.label("subject_id") if hasattr(subject_id, "label") else literal(subject_id).label("subject_id"),
        literal(message).label("message"),
        literal(details, JSONB).label("details"),
        literal(occurred_at, Event.occurred_at.type).label("occurred_at"),
    )
    return (
        pg_insert(Event)
        .from_select(columns, values)
        .on_conflict_do_nothing(
            index_elements=[
                Event.cluster_id,
                Event.type,
                Event.subject_type,
                Event.occurred_at,
                func.md5(Event.message),
            ]
        )
    )


def insert_node_event(cluster_id: UUID, payload: NodeEvent) -> Insert:
    return _insert_event(
        cluster_id,
        f"node.{payload.event}",
        EVENT_SEVERITY.get(payload.event, "info"),
        "node",
        _node_ref(cluster_id, payload.node_id),
        payload.message,
        {"reason": payload.reason},
        parse_ts(payload.occurred_at),
    )


def mark_node_left(cluster_id: UUID, node_uid: str, moment: Any) -> Update:
    """Records that a node the cluster reported as gone is gone.

    The operator diffs consecutive projections and says so the moment a node
    disappears, but until this existed the plane filed that as an event and left
    the row claiming the node was ready. The only thing that could correct it was
    a prune, and a prune needs a completed resync — so a departed node stayed
    placeable for as long as the session happened to last.

    Marked rather than deleted, because removal is the prune's job and doing it
    twice in two ways invites the two to disagree. `condition` already carries
    membership for exactly this: `node_condition` maps any membership that is not
    `joined` onto the condition, so `left` is a value this column was built to
    hold. The row survives until the next complete snapshot retires it, but it
    stops claiming to be somewhere to put work.
    """
    return (
        update(Node)
        .where(Node.cluster_id == cluster_id, Node.uid == observed_uid(node_uid, NODE))
        .values(condition="left", ready=False, observed_at=moment)
    )


def orphan_devices(cluster_id: UUID, node_uid: str, moment: Any) -> Update:
    """Stops a departed node's devices claiming health they cannot still have.

    A device is only observable through its node. Once the node is gone nothing
    can report on the device again, so leaving it `healthy` states a fact whose
    only source has disappeared — and `healthy` is half of what makes a device
    free to place on.
    """
    return (
        update(Device)
        .where(
            Device.cluster_id == cluster_id,
            Device.node_id == _node_ref(cluster_id, node_uid),
        )
        .values(health="unknown", allocatable=False, observed_at=moment)
    )


def insert_device_event(cluster_id: UUID, payload: DeviceEvent) -> Insert:
    return _insert_event(
        cluster_id,
        f"device.{payload.event}",
        EVENT_SEVERITY.get(payload.event, "info"),
        "device",
        select(Device.id).where(Device.cluster_id == cluster_id, Device.uid == payload.device_id).scalar_subquery(),
        payload.message,
        {"reason": payload.reason, "node_id": payload.node_id},
        parse_ts(payload.occurred_at),
    )


def insert_replica_event(cluster_id: UUID, payload: ReplicaEvent) -> Insert:
    return _insert_event(
        cluster_id,
        f"replica.{payload.event}",
        EVENT_SEVERITY.get(payload.event, "info"),
        "replica",
        None,
        payload.message,
        {"reason": payload.reason, "replica_id": payload.replica_id, "deployment_id": payload.deployment_id},
        parse_ts(payload.occurred_at),
    )


DEPLOYMENT_PHASE_SEVERITY = {
    "pending": "info",
    "admitted": "info",
    "placed": "info",
    "running": "info",
    "succeeded": "info",
    "degraded": "warning",
    "failed": "error",
    "terminating": "info",
}


DEPLOYMENT_REASON_SEVERITY = {
    "NoSchedulableCapacity": "warning",
    "RenderFailed": "error",
    "PayloadUndecodable": "error",
    "FingerprintMismatch": "warning",
    "LiveObjectUnreadable": "warning",
    # What the cluster found between the apply and a running replica. `PodUnschedulable` is a
    # warning because it clears on its own when a node joins; the rest do not.
    "PodUnschedulable": "warning",
    "ClusterRejected": "error",
    "ImagePullFailed": "error",
    "CrashLooping": "error",
    "ContainerConfigInvalid": "error",
    "NoReplicasStarted": "error",
}
"""Reasons whose severity is not the one their phase implies.

A reconcile that concluded `pending` because the cluster has nowhere to put the
work is not the same news as one that concluded `pending` because weights are
still downloading, and the phase alone cannot tell them apart.
"""


def deployment_status_before(deployment_id: UUID) -> Select:
    """What was last observed about a deployment, read before the new frame overwrites it.

    The status row is last-writer-wins by design, so the only place a transition
    exists is between this read and the upsert that follows it. Everything the
    console draws as history is derived here or nowhere.
    """
    return select(
        DeploymentStatus.phase,
        DeploymentStatus.reason,
        DeploymentStatus.message,
        DeploymentStatus.ready_replicas,
        DeploymentStatus.observed_generation,
        DeploymentStatus.last_error_code,
    ).where(DeploymentStatus.deployment_id == deployment_id)


def deployment_progressed(
    cluster_id: UUID,
    deployment_id: UUID,
    payload: DeploymentObserved,
) -> Insert:
    """Records one reconcile conclusion as an event.

    The operator already suppresses a report that would repeat its last one, so a
    frame arriving here is a transition rather than a heartbeat, and filing each
    one is what turns a status row that only ever shows the present into the
    history of how a deployment got there. Before this, a deployment that spent
    forty-seven minutes on `WeightsNotResident` and then landed left no trace of
    the forty-seven minutes anywhere the plane could show.

    `subject_id` is the deployment rather than null: unlike a pruned node, the row
    this points at outlives the event.
    """
    severity = DEPLOYMENT_REASON_SEVERITY.get(
        payload.reason or "",
        DEPLOYMENT_PHASE_SEVERITY.get(payload.phase, "info"),
    )
    return _insert_event(
        cluster_id,
        f"deployment.{payload.phase}",
        severity,
        "deployment",
        deployment_id,
        payload.message or payload.reason or f"deployment is {payload.phase}",
        {
            "reason": payload.reason or "",
            "phase": payload.phase,
            "generation": str(payload.generation),
            "ready_replicas": str(payload.ready_replicas),
            "desired_replicas": str(payload.desired_replicas),
            # The failing controller's own words, on the event and not only on the status row.
            # The row holds the present and is overwritten; the event is the only place a
            # failure that later cleared can still be read.
            **(
                {"error_code": payload.last_error.code, "error_message": payload.last_error.message}
                if payload.last_error
                else {}
            ),
        },
        parse_ts(payload.observed_at),
    )


CACHE_STATE_SEVERITY = {"absent": "info", "fetching": "info", "resident": "info", "evicting": "warn"}


def cache_progressed(cluster_id: UUID, artifact_id: UUID, payload: CacheObserved) -> Insert:
    """Records one artifact's residency transition as an event.

    Residency had a row and no timeline. The row answers "are the weights there
    now"; nothing answered "what has been happening to them", and that is the
    question a stuck deployment actually asks. A workload whose weights were
    never fetched — because nothing on that cluster named an engine image to
    fetch them with — showed as a deployment repeating `WeightsNotResident`
    forever, with the reason it would never stop repeating recorded nowhere the
    plane could show.

    `reason` is the operator's own word for why this state and not another, and
    it is free text on the wire: an old operator sends none and this files the
    state alone, which is what it did before.

    Filed against the artifact rather than the deployment because that is what it
    is about. One download serves every deployment of those weights on the
    cluster, and copying the same event onto each of them would make the fan-out
    look like a fact.
    """
    reason = getattr(payload, "reason", "") or ""
    message = getattr(payload, "message", "") or reason or f"weights are {payload.state}"
    return _insert_event(
        cluster_id,
        f"cache.{payload.state}",
        CACHE_STATE_SEVERITY.get(payload.state, "info"),
        "artifact",
        artifact_id,
        message,
        {
            "artifact_id": payload.artifact_id,
            "state": payload.state,
            "reason": reason,
            "bytes_resident": str(payload.bytes_resident),
        },
        parse_ts(payload.observed_at),
    )


def deployment_lifecycle(
    cluster_id: UUID | None,
    deployment_id: UUID,
    event: str,
    message: str,
    details: dict[str, str],
    occurred_at: Any,
    severity: str = "info",
) -> Insert:
    """Records something the plane itself did to a deployment.

    The other half of the same timeline. `deployment_progressed` covers what the
    cluster concluded; this covers what an operator of the product asked for —
    created, scaled, dispatched, deleted — so that a single query answers "what
    happened to this deployment" without the reader having to know that the two
    facts have different writers.
    """
    return _insert_event(
        cluster_id,
        f"deployment.{event}",
        severity,
        "deployment",
        deployment_id,
        message,
        details,
        occurred_at,
    )


def doomed_nodes(cluster_id: UUID, keep: set[str]) -> Select:
    """The nodes a prune is about to delete, read before they are gone.

    Read first and deleted second because the event has to outlive its subject.
    A node that vanishes from a complete snapshot is a node that left the cluster
    — a reclaimed spot instance, most often — and that is exactly the fact worth
    keeping once the row that carried it is deleted. The event therefore records
    the node's uid in `details` and leaves `subject_id` null: pointing a foreign
    key at a row this statement is about to remove would take the event with it.
    """
    return select(Node.uid, Node.name).where(Node.cluster_id == cluster_id, Node.uid.notin_(keep or {""}))


def doomed_workloads(cluster_id: UUID, keep: set[str]) -> Select:
    """The sightings a prune is about to forget. Same reasoning as `doomed_nodes`."""
    return select(DiscoveredWorkload.uid, DiscoveredWorkload.namespace, DiscoveredWorkload.name).where(
        DiscoveredWorkload.cluster_id == cluster_id, DiscoveredWorkload.uid.notin_(keep or {""})
    )


def insert_prune_event(cluster_id: UUID, subject_type: str, subject_uid: str, label: str, moment: Any) -> Insert:
    """Records that a complete snapshot stopped mentioning something.

    Raised by the plane rather than by a cluster, which is why the message says
    what the plane actually knows: the snapshot did not include it. The operator
    may well have sent a `node.left` too, and the two are not duplicates — that
    one is the cluster reporting an event, this one is the plane noticing an
    absence, and an incident where only the second exists is the interesting one.
    """
    return _insert_event(
        cluster_id,
        f"{subject_type}.removed",
        EVENT_SEVERITY.get("left", "warning"),
        subject_type,
        None,
        f"{label} is no longer reported by its cluster",
        {"reason": "absent_from_snapshot", f"{subject_type}_id": subject_uid},
        moment,
    )


def prune_deployment_replicas(cluster_id: UUID, deployment_id: UUID, keep: list[str]) -> Delete:
    """Forgets replicas one cluster no longer reports for one deployment.

    Not a resync prune and deliberately narrower than one: `deployment.observed`
    names the whole replica set of a single deployment on a single cluster every
    time it is sent, so absence from that list is evidence about that deployment
    alone and nothing else. A truncated snapshot cannot cost anything here the
    way it could for nodes, because there is no snapshot — there is one frame,
    which either arrived whole or did not arrive.

    An empty `keep` deletes them all, which is what a deployment scaled to zero
    means. The caller is the one that distinguishes that from an operator too
    old to make the claim, and never calls this for the latter.

    Allocations cascade with the replica, which is the other half of the leak:
    the devices a departed pod held stop reading as committed.
    """
    keep_uids = {observed_uid(replica_id, REPLICA) for replica_id in keep}
    return delete(Replica).where(
        Replica.cluster_id == cluster_id,
        Replica.deployment_id == deployment_id,
        Replica.uid.notin_(keep_uids or {""}),
    )


def prune_nodes(cluster_id: UUID, keep: set[str]) -> Delete:
    return delete(Node).where(Node.cluster_id == cluster_id, Node.uid.notin_(keep or {""}))


def prune_devices(cluster_id: UUID, keep: set[str]) -> Delete:
    return delete(Device).where(Device.cluster_id == cluster_id, Device.uid.notin_(keep or {""}))


def prune_topology_domains(cluster_id: UUID, keep: set[str]) -> Delete:
    return delete(TopologyDomain).where(
        TopologyDomain.cluster_id == cluster_id, TopologyDomain.uid.notin_(keep or {""})
    )


def prune_discovered_workloads(cluster_id: UUID, keep: set[str]) -> Delete:
    """Forgets sightings a complete snapshot did not mention.

    Deleting the sighting does not delete the decision made about it: adoptions
    are keyed by the cluster's stable workload uid and outlive the row, so a
    workload that disappears for a resync and comes back is still tracked.
    """
    return delete(DiscoveredWorkload).where(
        DiscoveredWorkload.cluster_id == cluster_id, DiscoveredWorkload.uid.notin_(keep or {""})
    )


def prune_cluster_replicas(cluster_id: UUID, keep: set[str]) -> Delete:
    """Forgets every replica on this cluster a complete demand snapshot did not name.

    The gap this closes: demand-side observed state used to be pruned only per
    deployment, against the `replica_ids` on that deployment's own frame — which
    stops arriving the moment the deployment is torn down. A paused workload's
    replicas stayed `ready` for ever, the allocations under them stayed `held`,
    and the cards they named were never given back. Nothing that reads a
    transition can repair that, because the transition is exactly what a
    restarted operator did not see.

    Cluster-wide and guarded the way the supply side is: `decide` has already
    established that the snapshot arrived whole, so absence is evidence. The
    allocations cascade, both from here and from the deployment above.
    """
    return delete(Replica).where(Replica.cluster_id == cluster_id, Replica.uid.notin_(keep or {""}))


def prune_cluster_allocations(cluster_id: UUID, keep: set[str]) -> Delete:
    """Releases every allocation on this cluster a complete demand snapshot did not name.

    Mostly redundant with the replica prune above, because an allocation
    cascades with its replica — and deliberately kept anyway. An allocation
    whose replica the plane never recorded has nothing to cascade from, and that
    is precisely the row that used to hold a card for ever.
    """
    return delete(Allocation).where(Allocation.cluster_id == cluster_id, Allocation.uid.notin_(keep or {""}))


def upsert_sync_state(cluster_id: UUID, values: dict[str, Any], bump: bool = False) -> Insert:
    """Writes the observed half of a cluster.

    `Cluster.phase` is declared and is never touched from here; the two halves
    have one writer each, which is the reason the split exists at all.
    `observed_generation` counts frames applied, so it is incremented on the
    conflict path rather than taken from the insert values.
    """
    insert_values: dict[str, Any] = {"cluster_id": cluster_id, **values}
    if bump:
        insert_values["observed_generation"] = 1
    statement = pg_insert(ClusterSyncState).values(**insert_values)
    updates: dict[str, Any] = {key: statement.excluded[key] for key in values}
    if bump:
        updates["observed_generation"] = ClusterSyncState.observed_generation + 1
    return statement.on_conflict_do_update(index_elements=[ClusterSyncState.cluster_id], set_=updates)
