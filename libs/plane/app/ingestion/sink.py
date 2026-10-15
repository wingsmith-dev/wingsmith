"""The database-backed `Sink`: observed state from a live cluster, landing in Postgres.

Session per frame, by design. The hub is its own process with no request scope,
so there is nothing to borrow a session from; holding one for the life of a
connection would put a transaction open for hours on the highest-frequency
write path in the system, and the API would be the thing that noticed.

Raising is the whole error contract. `OperatorSession` withholds the
acknowledgement for any frame whose sink call raised and the operator replays
it, so a method here either commits or raises — never logs and returns, which
would acknowledge state that was never written.
"""

import time
import datetime as dt

from uuid import UUID
from typing import Any
from logging import getLogger

from sqlalchemy import select
from sqlalchemy.exc import DataError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.hub.sink import ClaimLost
from app.hub.sink import SinkFrame
from app.db.engine import session_factory
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Replica
from app.db.models import Artifact
from app.db.models import Allocation
from app.db.models import Deployment
from app.db.models import HubClusterClaim
from app.ingestion import statements
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
from app.ingestion.time import now
from app.ingestion.time import parse_ts
from app.ingestion.resync import NODE
from app.ingestion.resync import DEVICE
from app.ingestion.resync import REPLICA
from app.ingestion.resync import TOPOLOGY
from app.ingestion.resync import WORKLOAD
from app.ingestion.resync import ALLOCATION
from app.ingestion.resync import ResyncEpoch
from app.ingestion.resync import decide
from app.ingestion.statements import UnknownCluster
from app.ingestion.statements import MalformedObservation
from app.modules.benchmarks.follow import follows_operation


logger = getLogger(__name__)


PERMANENT = (MalformedObservation, IntegrityError, DataError)
"""Failures a replay cannot fix, because the frame and the rows it names are what they are.

A constraint violation is one: a component nobody declared, a device whose node was
never reported, an identifier claimed by another cluster. Withholding the ack for one
of these wedged the stream behind it until a person intervened, and the frames it
held back were the ones that would have repaired the state — the node frame after
its device, the release after the claim. Transient failures (a dropped connection, a
timeout, a serialisation conflict) are not in here and are retried by the session.
"""

TERMINAL_OPERATION_PHASES = statements.TERMINAL_OPERATION_PHASES
"""Restated in `statements` rather than imported from `app.modules.operations`: ingestion must not
depend on a module's service layer, and this is the one fact it needs from it."""

SYNC_STAMP_SECONDS = 1.0
"""How often one cluster's frames restamp its sync-state row. Every frame used to, which made that
one row the hottest in the database during a snapshot of thousands of frames."""

MAX_OPEN_EPOCHS = 4
"""Open snapshots one cluster may have at once. A peer only ever needs one; the rest are leaks."""

SUPPLY_SCOPES = frozenset({"supply", "all"})
DEMAND_SCOPES = frozenset({"demand", "all"})


def _roles(roles: Any) -> tuple:
    """The per-role counts as a comparable tuple, from either the stored row or the frame.

    One function for both sides because they are the same fact in two shapes — a list of dicts
    read back out of JSONB, and a list of Structs decoded off the wire — and comparing them
    directly would be true only by accident.
    """
    out = []
    for role in roles or ():
        if isinstance(role, dict):
            out.append((role.get("role"), role.get("desired"), role.get("ready")))
        else:
            out.append((role.role, role.desired, role.ready))
    return tuple(out)


def _moved(before: Any, payload: DeploymentObserved) -> bool:
    """Whether this frame says anything the last one did not.

    The operator already suppresses a report identical to its own previous one,
    but that memory is per process and does not survive a restart or a failover
    — after either, the first frame for every deployment restates whatever was
    already true. Comparing against the stored row instead is what keeps a hub
    reconnect from filing a page of events that record nothing having happened.

    Replica counts are part of the comparison because a rollout moves them
    without moving the phase, and "two of four ready" becoming "four of four" is
    the transition somebody watching a deploy is actually waiting on.
    """
    if before is None:
        return True
    error = payload.last_error.code if payload.last_error else ""
    return (
        before.phase != payload.phase
        or (before.reason or "") != (payload.reason or "")
        or (before.message or "") != (payload.message or "")
        or before.ready_replicas != payload.ready_replicas
        # A role can go short while the totals hold still: a prefill worker dying as a decode
        # worker comes ready leaves `2 of 3` unchanged and takes the deployment from
        # degraded-and-serving to serving nothing.
        or _roles(before.roles) != _roles(payload.roles)
        # A diagnosis can arrive without moving any of the above: the first frame that explains
        # a shortfall carries the phase, reason and counts the shortfall already had.
        or (before.last_error_code or "") != error
    )


#: Phases a deployment reaches only after its work has been handed to the cluster. Reaching one of
#: them is what makes an earlier stage of the same generation impossible rather than merely old.
SETTLED_PHASES = frozenset({"placed", "running", "degraded"})

#: The reasons a `pending` frame can carry that describe a step on the way to applying rather than
#: a state the deployment has come to rest in. They are the operator's `stageRank` keys, and its
#: `stage` helper is the only thing that sends them.
STAGE_REASONS = frozenset({"Accepted", "Rendering"})


def _regressed(before, payload) -> bool:
    """Whether this frame describes a step the deployment has already moved past.

    The operator suppresses a stage it has already sent, but that memory is per
    process: a restart, a failover or a fresh hub session leaves it empty, and the
    next pass replays `Accepted` and `Rendering` for a deployment that has been
    serving for a week. The frames are not wrong — they are what the loop does on
    its first pass — they are simply about a moment that has passed.

    Applied blindly they cost more than a noisy timeline. The status row is
    last-writer-wins, so a replayed `pending` overwrites `running 1/1` and the
    console shows a healthy deployment as waiting for its weights until the next
    pass puts it back. Every hub reconnect produced that flicker.

    What makes a frame stale is its reason, not its phase. A stage is true once
    per instruction and the operator walks the set once per generation, so a
    stage arriving for a generation already placed is stale by definition. A
    `pending` that names a *state* — `NoSchedulableCapacity`, `WeightsNotResident`
    — is a fact about now, and the generation cannot move to tell it apart from a
    replay: nothing was redeployed. Judging those two alike is what let a cluster
    that lost its nodes report `NoSchedulableCapacity` every fifteen seconds for
    hours into a guard that dropped every frame, while the plane went on showing
    the deployment as running four of four.
    """
    if before is None or payload.phase != "pending":
        return False
    if (payload.reason or "") not in STAGE_REASONS:
        return False
    if before.observed_generation != payload.generation:
        return False
    return before.phase in SETTLED_PHASES


class DatabaseSink:
    """Writes observed state to Postgres. The only writer those tables have."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions
        self._stamps: dict[UUID, tuple[float, int, dt.datetime | None]] = {}
        """Per cluster: when its sync state was last stamped, how many frames have landed since,
        and when the latest of them did."""
        self._epochs: dict[str, dict[str, tuple[str | None, ResyncEpoch]]] = {}
        """Open snapshots, by cluster and then by request, with the session each arrived on.

        Keyed by cluster so a `reply_to` can only ever reach its own cluster's snapshot,
        bounded per cluster, and dropped when the session that opened them goes away —
        an epoch nothing will ever close used to stay in memory for the life of the hub.
        """

    # ----------------------------------------------------------------- sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def _cluster_id(self, db: AsyncSession, cluster_ref: str) -> UUID:
        """Resolves a wire cluster id to a primary key, in the write's own transaction.

        Accepts either a uid or a name, the same way the REST layer does — an
        operator is configured by hand, long before anyone has read back the uid
        the plane minted, and a name someone chose is the only identity a cluster
        has at that point.

        Not cached. A cluster that is deleted and registered again under the same
        name has a new primary key, and a process that remembered the old one wrote
        every frame against a row that no longer existed until it restarted. One
        indexed lookup per frame is the price of never being wrong about it.
        """
        result = await db.execute(select(Cluster.id).where(statements.cluster_ref_clause(cluster_ref)))
        cluster_id = result.scalar_one_or_none()
        if cluster_id is None:
            raise UnknownCluster(cluster_ref)
        return cluster_id

    async def _stamp(self, db: AsyncSession, cluster_id: UUID) -> None:
        """Records that a frame landed, writing the sync-state row at most once a `SYNC_STAMP_SECONDS`.

        The frames in between are not lost from the count: they are written by the
        next frame past the interval, or, once frames stop, by the session's next
        liveness write (`_sync_state`), which comes every `HUB_OWNERSHIP_CHECK_SECONDS`.
        So `last_sync_at` and `observed_generation` trail the last frame by at most
        the larger of the two intervals, not until whenever the next frame arrives.
        """
        moment = time.monotonic()
        last, pending, _ = self._stamps.get(cluster_id, (float("-inf"), 0, None))
        pending += 1
        if moment - last < SYNC_STAMP_SECONDS:
            self._stamps[cluster_id] = (last, pending, now())
            return
        await db.execute(statements.upsert_sync_state(cluster_id, {"last_sync_at": now()}, bump=pending))
        self._stamps[cluster_id] = (moment, 0, None)

    def _take_unstamped(self, cluster_id: UUID) -> tuple[int, dt.datetime | None]:
        """Hands over the frames `_stamp` has counted but not written, and when the last one landed."""
        last, pending, landed = self._stamps.get(cluster_id, (float("-inf"), 0, None))
        if pending:
            self._stamps[cluster_id] = (last, 0, None)
        return pending, landed

    def _restore_unstamped(self, cluster_id: UUID, pending: int, landed: dt.datetime | None) -> None:
        last, more, later = self._stamps.get(cluster_id, (float("-inf"), 0, None))
        self._stamps[cluster_id] = (last, pending + more, later or landed)

    async def _enter(self, db: AsyncSession, cluster_ref: str, session_id: str | None) -> UUID:
        """Opens a write for one cluster: resolve it, serialise on it, and check the claim.

        Serialised per cluster with a transaction-scoped advisory lock. The upserts
        below are idempotent one at a time, but an `ON CONFLICT` names one arbiter,
        and an observed table has two unique keys — the natural key and the
        cluster-minted `uid`. Two transactions inserting the same object at once
        both pass the arbiter's check, and the second fails on the other key
        instead of updating. One cluster's frames normally arrive on one session
        in order, so the lock costs nothing then; it is there for a replay
        overlapping a live frame, or a displaced session's last write.
        """
        cluster_id = await self._cluster_id(db, cluster_ref)
        await db.execute(statements.serialise_cluster(cluster_id))
        await self._fence(db, cluster_id, cluster_ref, session_id)
        return cluster_id

    async def _holds(self, db: AsyncSession, cluster_id: UUID, session_id: str) -> bool:
        """Whether `session_id` holds the cluster's claim, read under a share lock on the claim row.

        The lock is what makes the answer last the whole transaction. A newer `hello`
        claiming the cluster updates that row, so it waits for this write to commit,
        and every write after the claim moved sees the new owner and is refused. That
        is the single-writer guarantee ADR 0005 asks for; polling alone left a
        displaced session writing for up to one poll interval.
        """
        owner = await db.scalar(
            select(HubClusterClaim.session_id)
            .join(Cluster, Cluster.uid == HubClusterClaim.cluster_id)
            .where(Cluster.id == cluster_id)
            .with_for_update(read=True, of=HubClusterClaim)
        )
        return owner == session_id

    async def _fence(self, db: AsyncSession, cluster_id: UUID, frame_cluster: str, session_id: str | None) -> None:
        """Refuses the write unless the session it arrived on still owns the cluster.

        A frame with no session — a test, a caller outside the hub — is not fenced.
        """
        if session_id is not None and not await self._holds(db, cluster_id, session_id):
            raise ClaimLost(frame_cluster)

    # ----------------------------------------------------------------- references

    async def _uid_ref(self, db: AsyncSession, model: Any, uid: str) -> UUID | None:
        """The primary key behind a public id, or None if the plane has never heard of it."""
        result = await db.execute(select(model.id).where(model.uid == uid))
        return result.scalar_one_or_none()

    async def _deployment(self, db: AsyncSession, cluster_id: UUID, uid: str) -> tuple[UUID, UUID] | None:
        """`(deployment_id, workload_id)` for a declared deployment in this cluster.

        Scoped to the cluster deliberately: a frame naming a deployment that lives
        somewhere else is not a deployment this stream may write about.
        """
        result = await db.execute(
            select(Deployment.id, Deployment.workload_id).where(
                Deployment.cluster_id == cluster_id, Deployment.uid == uid
            )
        )
        row = result.first()
        return (row[0], row[1]) if row else None

    async def _allocation(
        self, db: AsyncSession, cluster_id: UUID, cluster_uid: str, payload: AllocationObserved
    ) -> bool:
        """Writes one allocation and reconciles its device membership in the same transaction.

        Three statements rather than one because an allocation is a row and a set:
        the row says what is committed and to whom, the set says which accelerators,
        and a device dropped from the set has to be stamped released — that is what
        frees it for the next allocation under the partial unique index.

        A frame naming a deployment or project the plane does not have is accepted
        and dropped, not replayed. Ingestion may not create either one — both are
        declared — and withholding the ack would wedge the stream behind a frame
        that can never be applied.

        A frame naming no accelerators is a release and takes the other path
        entirely: there is no device to read a class off, so the upsert would
        write a null into a column that refuses one and take the whole
        transaction down — including the release it was sent to perform.
        """
        deployment = await self._deployment(db, cluster_id, payload.deployment_id)
        project_id = await self._uid_ref(db, Project, payload.project_id)
        if deployment is None or project_id is None:
            logger.info(
                "ingestion.allocation.unresolved allocation=%s deployment=%s project=%s cluster=%s",
                payload.allocation_id,
                payload.deployment_id,
                payload.project_id,
                cluster_uid,
            )
            return False

        if not payload.device_ids:
            return await self._release(db, cluster_id, cluster_uid, payload)

        replica_id = await self._uid_ref(db, Replica, payload.replica_id)
        statement = statements.upsert_allocation(
            cluster_id, deployment[1], deployment[0], project_id, replica_id, payload
        ).returning(Allocation.id)
        allocation_id = (await db.execute(statement)).scalar_one_or_none()
        if allocation_id is None:
            logger.warning(
                "ingestion.allocation.foreign allocation=%s cluster=%s",
                payload.allocation_id,
                cluster_uid,
            )
            return False
        await db.execute(statements.release_allocation_devices(allocation_id, cluster_id, payload))
        await db.execute(statements.release_devices_held_elsewhere(allocation_id, cluster_id, payload))
        await db.execute(statements.attach_allocation_devices(allocation_id, cluster_id, payload))
        return True

    async def _release(self, db: AsyncSession, cluster_id: UUID, cluster_uid: str, payload: AllocationObserved) -> bool:
        """Hands back everything one allocation was holding.

        The cluster states a departure by restating the allocation with no
        devices, which is the only signal the plane gets that a replica is gone —
        demand state is not reaped by the supply resync, so an allocation nobody
        released stays held and its accelerators stay spoken for.

        An allocation the plane has never seen is accepted and dropped. There is
        nothing to release, and inserting a row to mark it finished would invent
        a commitment that never existed here.
        """
        result = await db.execute(
            select(Allocation.id).where(Allocation.cluster_id == cluster_id, Allocation.uid == payload.allocation_id)
        )
        allocation_id = result.scalar_one_or_none()
        if allocation_id is None:
            logger.info(
                "ingestion.allocation.release_unknown allocation=%s cluster=%s",
                payload.allocation_id,
                cluster_uid,
            )
            return True
        await db.execute(statements.release_allocation_devices(allocation_id, cluster_id, payload))
        await db.execute(statements.release_allocation(allocation_id, payload))
        return True

    # ----------------------------------------------------------------- observed

    async def apply_observed(self, frame: SinkFrame) -> None:
        """Applies one observed frame, or refuses it in one of two different ways.

        Raising withholds the ack and asks for a replay, which is the right
        answer only when a replay could succeed. A poison frame would come back
        unchanged forever and the stream behind it would never advance, so it is
        accepted, discarded and logged instead (PROTOCOL.md 6b): a malformed
        payload, and a frame the database refuses on a constraint (`PERMANENT`).
        The resync epoch is poisoned either way — a frame that did not land is not
        evidence about what exists.
        """
        try:
            tracked = await self._write(frame)
        except PERMANENT as exc:
            self._poison(frame)
            self._dead_letter(frame, exc)
            return
        except Exception:
            self._poison(frame)
            raise
        self._record(frame, tracked)

    @staticmethod
    def _dead_letter(frame: SinkFrame, exc: Exception) -> None:
        """Where a discarded frame goes: the log, at error, with enough to find it again."""
        logger.error(
            "ingestion.rejected cluster=%s type=%s seq=%s id=%s reason=%s",
            frame.cluster_id,
            frame.type,
            frame.seq,
            frame.id,
            getattr(exc, "orig", None) or exc,
        )

    async def _write(self, frame: SinkFrame) -> tuple[str, str] | None:
        async with self.sessions.begin() as db:
            cluster_id = await self._enter(db, frame.cluster_id, frame.session_id)
            tracked = await self._dispatch(db, cluster_id, frame)
            await self._stamp(db, cluster_id)
            return tracked

    async def _dispatch(self, db: AsyncSession, cluster_id: UUID, frame: SinkFrame) -> tuple[str, str] | None:
        """Applies one observed frame and reports what it identified, if anything.

        The return value is what a resync epoch remembers, so a type that is not
        yet ingested returns `None` and is counted but never pruned against.
        """
        payload = frame.payload
        match frame.type:
            case "node.observed":
                assert isinstance(payload, NodeObserved)
                await db.execute(statements.upsert_node(cluster_id, payload))
                return NODE, statements.observed_uid(payload.node_id, "nod_")
            case "device.observed":
                assert isinstance(payload, DeviceObserved)
                await db.execute(statements.upsert_device(cluster_id, payload))
                return DEVICE, statements.observed_uid(payload.device_id, "dev_")
            case "topology.observed":
                assert isinstance(payload, TopologyObserved)
                await db.execute(statements.upsert_topology_domain(cluster_id, payload))
                for statement in statements.replace_domain_nodes(cluster_id, payload):
                    await db.execute(statement)
                if payload.device_ids:
                    await db.execute(statements.attach_domain_devices(cluster_id, payload))
                return TOPOLOGY, statements.observed_uid(payload.domain_id, "tpd_")
            case "component.observed":
                assert isinstance(payload, ComponentObserved)
                await db.execute(statements.upsert_component_status(cluster_id, payload))
                return None
            case "workload.discovered":
                assert isinstance(payload, WorkloadDiscovered)
                await db.execute(statements.upsert_discovered_workload(cluster_id, payload))
                return WORKLOAD, statements.observed_uid(payload.workload_id, "dsw_")
            case "deployment.observed":
                assert isinstance(payload, DeploymentObserved)
                deployment = await self._deployment(db, cluster_id, payload.deployment_id)
                if deployment is None:
                    logger.info(
                        "ingestion.deployment.unknown deployment=%s cluster=%s",
                        payload.deployment_id,
                        frame.cluster_id,
                    )
                    return None
                # Read, then file, then overwrite. The status row is last-writer-wins, so the
                # transition only exists in the gap between the read and the upsert — after it
                # there is nothing left to compare against.
                before = (await db.execute(statements.deployment_status_before(deployment[0]))).first()
                if _regressed(before, payload):
                    logger.debug(
                        "ingestion.deployment.stale_stage deployment=%s generation=%s phase=%s reason=%s cluster=%s",
                        payload.deployment_id,
                        payload.generation,
                        before.phase,
                        payload.reason,
                        frame.cluster_id,
                    )
                    return None
                if _moved(before, payload):
                    await db.execute(statements.deployment_progressed(cluster_id, deployment[0], payload))
                await db.execute(statements.upsert_deployment_status(cluster_id, deployment[0], payload))
                # The frame names this deployment's whole replica set on this cluster, so the
                # replicas it does not name are gone. Nothing else ever removes one: demand
                # state is not pruned against a resync, so a rolled or restarted pod left its
                # row behind for ever and kept counting towards the rollup — which is how a
                # deployment of one came to read three ready of one in the console.
                #
                # `None` is an operator too old to make the claim and prunes nothing; an empty
                # list is a claim of none and deletes them all, which is a scale to zero.
                if payload.replica_ids is not None:
                    pruned = await db.execute(
                        statements.prune_deployment_replicas(cluster_id, deployment[0], payload.replica_ids)
                    )
                    if pruned.rowcount:
                        logger.info(
                            "ingestion.deployment.replicas_pruned deployment=%s cluster=%s removed=%d kept=%d",
                            payload.deployment_id,
                            frame.cluster_id,
                            pruned.rowcount,
                            len(payload.replica_ids),
                        )
                # Admission moves with it, on this deployment alone. A workload has one of these
                # per cluster and they do not agree — one region can be running while another sits
                # queued on quota — so the phase is written here and the workload only takes the
                # rollup that is genuinely global: when the first of its clusters started.
                advance = statements.advance_admission_phase(deployment[0], payload)
                if advance is not None:
                    await db.execute(advance)
                    if statements.ADMISSION_PHASE_FOR_OBSERVED.get(payload.phase) == "running":
                        await db.execute(statements.roll_up_workload_started(deployment[1], payload))
                return None
            case "replica.observed":
                assert isinstance(payload, ReplicaObserved)
                deployment = await self._deployment(db, cluster_id, payload.deployment_id)
                if deployment is None:
                    logger.info(
                        "ingestion.replica.unknown_deployment replica=%s deployment=%s cluster=%s",
                        payload.replica_id,
                        payload.deployment_id,
                        frame.cluster_id,
                    )
                    return None
                await db.execute(statements.upsert_replica(cluster_id, deployment[0], payload))
                return REPLICA, statements.observed_uid(payload.replica_id, "rep_")
            case "allocation.observed":
                assert isinstance(payload, AllocationObserved)
                if not await self._allocation(db, cluster_id, frame.cluster_id, payload):
                    return None
                return ALLOCATION, statements.observed_uid(payload.allocation_id, "alc_")
            case "cache.observed":
                assert isinstance(payload, CacheObserved)
                artifact_id = await self._uid_ref(db, Artifact, payload.artifact_id)
                if artifact_id is None:
                    logger.info(
                        "ingestion.cache.unknown_artifact artifact=%s cluster=%s",
                        payload.artifact_id,
                        frame.cluster_id,
                    )
                    return None
                for statement in statements.upsert_cache_entries(cluster_id, artifact_id, payload):
                    await db.execute(statement)
                await db.execute(statements.cache_progressed(cluster_id, artifact_id, payload))
                return None
            case "benchmark.observed":
                assert isinstance(payload, BenchmarkObserved)
                result = await db.execute(statements.update_benchmark_run(cluster_id, payload))
                if result.rowcount == 0:
                    logger.info(
                        "ingestion.benchmark.unknown benchmark=%s cluster=%s",
                        payload.benchmark_id,
                        frame.cluster_id,
                    )
                return None
            case "cluster.observed":
                assert isinstance(payload, ClusterObserved)
                await db.execute(statements.upsert_cluster_observed(cluster_id, payload))
                return None
            case "capacity.observed":
                return None
            case _:
                logger.debug("ingestion.observed.unhandled type=%s cluster=%s", frame.type, frame.cluster_id)
                return None

    # ----------------------------------------------------------------- events

    async def record_event(self, frame: SinkFrame) -> None:
        """Records one event, discarding it the way `apply_observed` does when it can never apply."""
        try:
            await self._record_event(frame)
        except PERMANENT as exc:
            self._dead_letter(frame, exc)

    async def _record_event(self, frame: SinkFrame) -> None:
        payload = frame.payload
        async with self.sessions.begin() as db:
            cluster_id = await self._enter(db, frame.cluster_id, frame.session_id)
            match frame.type:
                case "node.event":
                    assert isinstance(payload, NodeEvent)
                    await db.execute(statements.insert_node_event(cluster_id, payload))
                    # The event is inserted first so its subject still resolves: the statements
                    # below change what the node is, and one of them takes its devices with it.
                    if payload.event == "left":
                        moment = parse_ts(payload.occurred_at)
                        await db.execute(statements.orphan_devices(cluster_id, payload.node_id, moment))
                        await db.execute(statements.mark_node_left(cluster_id, payload.node_id, moment))
                        logger.info(
                            "ingestion.node.left cluster=%s node=%s reason=%s",
                            frame.cluster_id,
                            payload.node_id,
                            payload.reason,
                        )
                case "device.event":
                    assert isinstance(payload, DeviceEvent)
                    await db.execute(statements.insert_device_event(cluster_id, payload))
                case "replica.event":
                    assert isinstance(payload, ReplicaEvent)
                    await db.execute(statements.insert_replica_event(cluster_id, payload))
                case _:
                    logger.debug("ingestion.event.unhandled type=%s", frame.type)
                    return
            await self._stamp(db, cluster_id)

    async def record_operation(self, frame: SinkFrame) -> None:
        """Records operation progress, discarding it the way `apply_observed` does when it can never apply."""
        try:
            await self._record_operation(frame)
        except PERMANENT as exc:
            self._dead_letter(frame, exc)

    async def _record_operation(self, frame: SinkFrame) -> None:
        payload = frame.payload
        if not isinstance(payload, OperationObserved):
            logger.debug("ingestion.operation.unhandled type=%s", frame.type)
            return
        async with self.sessions.begin() as db:
            cluster_id = await self._enter(db, frame.cluster_id, frame.session_id)
            result = await db.execute(statements.update_operation(cluster_id, payload))
            if result.rowcount == 0:
                logger.info(
                    "ingestion.operation.unknown operation=%s cluster=%s", payload.operation_id, frame.cluster_id
                )
            elif payload.phase in TERMINAL_OPERATION_PHASES:
                # A benchmark reports its own result on `benchmark.observed`, and everything that
                # fails before the executor gets that far reports nothing at all — a refused kind,
                # an operator too old to know it, a cluster that never came back. Without this the
                # run stays open for ever with nothing saying why.
                await db.execute(
                    follows_operation(
                        payload.operation_id,
                        payload.phase,
                        payload.failure.message if payload.failure else None,
                    )
                )
            await self._stamp(db, cluster_id)

    # ----------------------------------------------------------------- resync

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str, session_id: str | None = None) -> None:
        open_epochs = self._epochs.setdefault(cluster_id, {})
        while len(open_epochs) >= MAX_OPEN_EPOCHS:
            stale = next(iter(open_epochs))
            open_epochs.pop(stale)
            logger.warning("ingestion.resync.evicted cluster=%s request=%s", cluster_id, stale)
        open_epochs[request_id] = (
            session_id,
            ResyncEpoch(cluster_uid=cluster_id, scope=scope, request_id=request_id),
        )
        logger.info("ingestion.resync.begin cluster=%s scope=%s request=%s", cluster_id, scope, request_id)

    async def end_resync(
        self, cluster_id: str, request_id: str, count: int, complete: bool, session_id: str | None = None
    ) -> None:
        epoch = self._close_epoch(cluster_id, request_id, session_id)
        decision = decide(epoch, count, complete)
        if not decision.prune or epoch is None:
            logger.warning(
                "ingestion.resync.no_prune cluster=%s request=%s reason=%s", cluster_id, request_id, decision.reason
            )
            return
        if epoch.scope in DEMAND_SCOPES:
            await self._prune_demand(cluster_id, epoch, session_id)
        if epoch.scope not in SUPPLY_SCOPES:
            return

        async with self.sessions.begin() as db:
            resolved = await self._enter(db, cluster_id, session_id)
            moment = now()
            going = (await db.execute(statements.doomed_nodes(resolved, epoch.present[NODE]))).all()
            leaving = (await db.execute(statements.doomed_workloads(resolved, epoch.present[WORKLOAD]))).all()
            await db.execute(statements.orphan_held_devices(resolved, epoch.present[DEVICE], moment))
            await db.execute(statements.retire_held_nodes(resolved, epoch.present[NODE], moment))
            devices = await db.execute(statements.prune_devices(resolved, epoch.present[DEVICE]))
            domains = await db.execute(statements.prune_topology_domains(resolved, epoch.present[TOPOLOGY]))
            workloads = await db.execute(statements.prune_discovered_workloads(resolved, epoch.present[WORKLOAD]))
            nodes = await db.execute(statements.prune_nodes(resolved, epoch.present[NODE]))
            for uid, name in going:
                await db.execute(statements.insert_prune_event(resolved, "node", uid, f"node {name}", moment))
            for uid, namespace, name in leaving:
                await db.execute(
                    statements.insert_prune_event(resolved, "workload", uid, f"workload {namespace}/{name}", moment)
                )
            await db.execute(statements.upsert_sync_state(resolved, {"last_sync_at": moment}))
        logger.info(
            "ingestion.resync.pruned cluster=%s received=%d nodes=%d devices=%d domains=%d workloads=%d",
            cluster_id,
            epoch.received,
            nodes.rowcount,
            devices.rowcount,
            domains.rowcount,
            workloads.rowcount,
        )

    async def _prune_demand(self, cluster_id: str, epoch: ResyncEpoch, session_id: str | None = None) -> None:
        """Forgets the replicas and allocations a complete demand snapshot did not name.

        The half that was missing, and the reason a paused workload kept holding
        cards. Demand state used to be reaped only by
        `deployment.observed.replica_ids` — a per-deployment claim, carried on a
        frame that stops arriving the moment the deployment is torn down. Every
        way of losing that frame left a row nothing would ever collect again: a
        pause, a delete, an operator restarted while the pod was going away.

        Cluster-wide and under the same guard as the supply side. `decide` has
        already established that the snapshot arrived whole and unpoisoned, so
        absence from it is evidence rather than silence. Nothing here reads a
        transition, which is the point: the cluster states what it holds and the
        plane converges on it, from any starting state and after any outage.
        """
        async with self.sessions.begin() as db:
            resolved = await self._enter(db, cluster_id, session_id)
            replicas = await db.execute(statements.prune_cluster_replicas(resolved, epoch.present[REPLICA]))
            allocations = await db.execute(statements.prune_cluster_allocations(resolved, epoch.present[ALLOCATION]))
        logger.info(
            "ingestion.resync.demand_pruned cluster=%s received=%d replicas=%d allocations=%d",
            cluster_id,
            epoch.received,
            replicas.rowcount,
            allocations.rowcount,
        )

    def _close_epoch(self, cluster_id: str, request_id: str, session_id: str | None) -> ResyncEpoch | None:
        """Takes one snapshot out of the open set. A different session's snapshot is not this one's to close."""
        open_epochs = self._epochs.get(cluster_id)
        if not open_epochs:
            return None
        entry = open_epochs.get(request_id)
        if entry is None or (session_id is not None and entry[0] not in (None, session_id)):
            return None
        open_epochs.pop(request_id)
        if not open_epochs:
            self._epochs.pop(cluster_id, None)
        return entry[1]

    def _forget_epochs(self, cluster_id: str, session_id: str | None) -> None:
        """Drops the snapshots a departed session left open. Nothing will ever close them."""
        open_epochs = self._epochs.get(cluster_id)
        if not open_epochs:
            return
        for request_id in [r for r, (owner, _) in open_epochs.items() if session_id is None or owner == session_id]:
            open_epochs.pop(request_id)
            logger.info("ingestion.resync.abandoned cluster=%s request=%s", cluster_id, request_id)
        if not open_epochs:
            self._epochs.pop(cluster_id, None)

    def _epoch_for(self, frame: SinkFrame) -> ResyncEpoch | None:
        if not frame.reply_to:
            return None
        entry = self._epochs.get(frame.cluster_id, {}).get(frame.reply_to)
        return entry[1] if entry else None

    def _record(self, frame: SinkFrame, tracked: tuple[str, str] | None) -> None:
        """Counts a frame into its epoch, whether or not the type is prunable.

        Every frame the snapshot carried has to be counted, because the count is
        checked against the sender's declared total. A type ingestion does not
        yet write still arrived.

        A live frame — one that answers no snapshot — still lands while a snapshot
        of its cluster is open, and what it wrote must survive that snapshot's
        prune. It is kept by every open epoch of its cluster without being counted.
        """
        epoch = self._epoch_for(frame)
        if epoch is None:
            if tracked is not None:
                for _, open_epoch in self._epochs.get(frame.cluster_id, {}).values():
                    open_epoch.keep(*tracked)
            return
        if tracked is None:
            epoch.received += 1
            return
        epoch.record(*tracked)

    def _poison(self, frame: SinkFrame) -> None:
        epoch = self._epoch_for(frame)
        if epoch is not None:
            epoch.poison()

    # ----------------------------------------------------------------- liveness

    async def mark_connected(
        self, cluster_uid: str, operator_version: str, session_id: str | None = None
    ) -> str | None:
        """The observed half of a cluster coming up. Never touches `Cluster.phase`.

        Returns the cluster's canonical uid, which the session needs and only a
        database can supply: an operator names itself however it was configured,
        and live dispatch is keyed by what writers hold, which is always the uid.
        """
        moment = now()
        return await self._sync_state(
            cluster_uid,
            {
                "reachable": True,
                "operator_version": operator_version or None,
                "connected_at": moment,
                "last_seen_at": moment,
                "last_error": None,
            },
            session_id,
        )

    async def mark_disconnected(
        self, cluster_uid: str, error: str | None = None, session_id: str | None = None
    ) -> None:
        """The observed half of a cluster going away, unless another session has already taken it over."""
        self._forget_epochs(cluster_uid, session_id)
        moment = now()
        await self._sync_state(
            cluster_uid,
            {"reachable": False, "disconnected_at": moment, "last_seen_at": moment, "last_error": error},
            session_id,
        )

    async def touch(self, cluster_uid: str, session_id: str | None = None) -> None:
        """Restates that the cluster is here. `reachable` too, so a failed `mark_connected` heals."""
        await self._sync_state(cluster_uid, {"reachable": True, "last_seen_at": now()}, session_id)

    async def _sync_state(self, cluster_uid: str, values: dict[str, Any], session_id: str | None = None) -> str | None:
        """Writes one cluster's sync state and reports the uid of the row it wrote.

        The uid is returned rather than assumed because `cluster_uid` is a
        reference and may be either identity; callers that already hold a uid pay
        nothing for it beyond the row they were reading anyway.

        Fenced on the claim when a session is named: liveness is the one fact a
        displaced session is most likely to get wrong, because its last act is to
        report the cluster it no longer serves as gone. Skipped silently rather
        than raised — the session is closing either way.

        Also writes whatever `_stamp` is still holding for the cluster, so the tail of
        a burst reaches the row within one liveness interval of its last frame.
        """
        cluster_id: UUID | None = None
        pending, landed = 0, None
        try:
            async with self.sessions.begin() as db:
                cluster_id = await self._cluster_id(db, cluster_uid)
                if session_id is not None and not await self._holds(db, cluster_id, session_id):
                    logger.info("ingestion.sync_state.fenced cluster=%s session=%s", cluster_uid, session_id)
                    return None
                pending, landed = self._take_unstamped(cluster_id)
                stamped = values if landed is None else {"last_sync_at": landed, **values}
                await db.execute(statements.upsert_sync_state(cluster_id, stamped, bump=pending))
                return await db.scalar(select(Cluster.uid).where(Cluster.id == cluster_id))
        except BaseException:
            if cluster_id is not None and pending:
                self._restore_unstamped(cluster_id, pending, landed)
            raise


class DatabaseDesiredSource:
    """Reads declared state out of Postgres to stream down to one cluster.

    A generator, not a list: a full resync of a large cluster is thousands of
    objects and the session writes them one at a time under backpressure, so
    materialising the snapshot to hand it over would buy nothing and cost the
    memory.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def iter_desired(self, cluster_id: str, scope: str):
        from app.ingestion.desired import stream_desired

        async with self.sessions() as db:
            async for item in stream_desired(db, cluster_id, scope):
                yield item

    async def iter_open_operations(self, cluster_id: str):
        """The cluster's unfinished operations, read the same way and sent separately.

        A session replays these on connect because an operation created while the
        cluster was offline has nothing else to carry it: the notification that
        would have dispatched it live found no session, and the resync that
        repairs declared state deliberately does not cover operations.
        """
        from app.ingestion.operations import open_operations

        async with self.sessions() as db:
            async for item in open_operations(db, cluster_id):
                yield item
