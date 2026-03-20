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

from uuid import UUID
from typing import Any
from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.hub.sink import SinkFrame
from app.db.engine import session_factory
from app.db.models import Cluster
from app.ingestion import statements
from app.hub.messages import NodeEvent
from app.hub.messages import DeviceEvent
from app.hub.messages import NodeObserved
from app.hub.messages import ReplicaEvent
from app.hub.messages import DeviceObserved
from app.hub.messages import TopologyObserved
from app.hub.messages import ComponentObserved
from app.hub.messages import OperationObserved
from app.hub.messages import WorkloadDiscovered
from app.ingestion.time import now
from app.ingestion.resync import NODE
from app.ingestion.resync import DEVICE
from app.ingestion.resync import TOPOLOGY
from app.ingestion.resync import WORKLOAD
from app.ingestion.resync import ResyncEpoch
from app.ingestion.resync import decide
from app.ingestion.statements import MalformedObservation


logger = getLogger(__name__)


SUPPLY_SCOPES = frozenset({"supply", "all"})


class UnknownCluster(Exception):
    """A frame arrived for a cluster with no registry entry.

    Raised rather than ignored: a cluster the plane has never heard of is not
    something ingestion may create — clusters are declared — and acknowledging
    its state would silently discard it.
    """

    def __init__(self, cluster_uid: str) -> None:
        super().__init__(f"no cluster registered as {cluster_uid}")
        self.cluster_uid = cluster_uid


class DatabaseSink:
    """Writes observed state to Postgres. The only writer those tables have."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions
        self._clusters: dict[str, UUID] = {}
        self._epochs: dict[str, ResyncEpoch] = {}

    # ----------------------------------------------------------------- sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def _cluster_id(self, db: AsyncSession, cluster_uid: str) -> UUID:
        """Resolves a wire cluster id to a primary key, memoised for the process.

        Safe to cache: the mapping is between two immutable identities, so the
        only way it goes stale is a cluster being deleted, which takes its
        observed rows with it by cascade.
        """
        cached = self._clusters.get(cluster_uid)
        if cached is not None:
            return cached
        result = await db.execute(select(Cluster.id).where(Cluster.uid == cluster_uid))
        cluster_id = result.scalar_one_or_none()
        if cluster_id is None:
            raise UnknownCluster(cluster_uid)
        self._clusters[cluster_uid] = cluster_id
        return cluster_id

    # ----------------------------------------------------------------- observed

    async def apply_observed(self, frame: SinkFrame) -> None:
        """Applies one observed frame, or refuses it in one of two different ways.

        Raising withholds the ack and asks for a replay, which is the right
        answer only when a replay could succeed. A malformed frame is a poison
        frame: it would come back unchanged forever and the stream behind it
        would never advance, so it is accepted, discarded and logged instead
        (PROTOCOL.md 6b). The resync epoch is poisoned either way — a frame that
        did not land is not evidence about what exists.
        """
        try:
            tracked = await self._write(frame)
        except MalformedObservation as exc:
            self._poison(frame)
            logger.error(
                "ingestion.rejected cluster=%s type=%s seq=%s id=%s reason=%s",
                frame.cluster_id,
                frame.type,
                frame.seq,
                frame.id,
                exc,
            )
            return
        except Exception:
            self._poison(frame)
            raise
        self._record(frame, tracked)

    async def _write(self, frame: SinkFrame) -> tuple[str, str] | None:
        async with self.sessions.begin() as db:
            cluster_id = await self._cluster_id(db, frame.cluster_id)
            tracked = await self._dispatch(db, cluster_id, frame)
            await db.execute(statements.upsert_sync_state(cluster_id, {"last_sync_at": now()}, bump=True))
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
            case "capacity.observed":
                return None
            case _:
                logger.debug("ingestion.observed.unhandled type=%s cluster=%s", frame.type, frame.cluster_id)
                return None

    # ----------------------------------------------------------------- events

    async def record_event(self, frame: SinkFrame) -> None:
        payload = frame.payload
        async with self.sessions.begin() as db:
            cluster_id = await self._cluster_id(db, frame.cluster_id)
            match frame.type:
                case "node.event":
                    assert isinstance(payload, NodeEvent)
                    await db.execute(statements.insert_node_event(cluster_id, payload))
                case "device.event":
                    assert isinstance(payload, DeviceEvent)
                    await db.execute(statements.insert_device_event(cluster_id, payload))
                case "replica.event":
                    assert isinstance(payload, ReplicaEvent)
                    await db.execute(statements.insert_replica_event(cluster_id, payload))
                case _:
                    logger.debug("ingestion.event.unhandled type=%s", frame.type)
                    return
            await db.execute(statements.upsert_sync_state(cluster_id, {"last_sync_at": now()}, bump=True))

    async def record_operation(self, frame: SinkFrame) -> None:
        payload = frame.payload
        if not isinstance(payload, OperationObserved):
            logger.debug("ingestion.operation.unhandled type=%s", frame.type)
            return
        async with self.sessions.begin() as db:
            cluster_id = await self._cluster_id(db, frame.cluster_id)
            result = await db.execute(statements.update_operation(cluster_id, payload))
            if result.rowcount == 0:
                logger.info(
                    "ingestion.operation.unknown operation=%s cluster=%s", payload.operation_id, frame.cluster_id
                )
            await db.execute(statements.upsert_sync_state(cluster_id, {"last_sync_at": now()}, bump=True))

    # ----------------------------------------------------------------- resync

    async def begin_resync(self, cluster_id: str, scope: str, request_id: str) -> None:
        self._epochs[request_id] = ResyncEpoch(cluster_uid=cluster_id, scope=scope, request_id=request_id)
        logger.info("ingestion.resync.begin cluster=%s scope=%s request=%s", cluster_id, scope, request_id)

    async def end_resync(self, cluster_id: str, request_id: str, count: int, complete: bool) -> None:
        epoch = self._epochs.pop(request_id, None)
        decision = decide(epoch, count, complete)
        if not decision.prune or epoch is None:
            logger.warning(
                "ingestion.resync.no_prune cluster=%s request=%s reason=%s", cluster_id, request_id, decision.reason
            )
            return
        if epoch.scope not in SUPPLY_SCOPES:
            logger.info("ingestion.resync.nothing_to_prune cluster=%s scope=%s", cluster_id, epoch.scope)
            return

        async with self.sessions.begin() as db:
            resolved = await self._cluster_id(db, cluster_id)
            devices = await db.execute(statements.prune_devices(resolved, epoch.present[DEVICE]))
            domains = await db.execute(statements.prune_topology_domains(resolved, epoch.present[TOPOLOGY]))
            workloads = await db.execute(statements.prune_discovered_workloads(resolved, epoch.present[WORKLOAD]))
            nodes = await db.execute(statements.prune_nodes(resolved, epoch.present[NODE]))
            await db.execute(statements.upsert_sync_state(resolved, {"last_sync_at": now()}))
        logger.info(
            "ingestion.resync.pruned cluster=%s received=%d nodes=%d devices=%d domains=%d workloads=%d",
            cluster_id,
            epoch.received,
            nodes.rowcount,
            devices.rowcount,
            domains.rowcount,
            workloads.rowcount,
        )

    def _epoch_for(self, frame: SinkFrame) -> ResyncEpoch | None:
        return self._epochs.get(frame.reply_to) if frame.reply_to else None

    def _record(self, frame: SinkFrame, tracked: tuple[str, str] | None) -> None:
        """Counts a frame into its epoch, whether or not the type is prunable.

        Every frame the snapshot carried has to be counted, because the count is
        checked against the sender's declared total. A type ingestion does not
        yet write still arrived.
        """
        epoch = self._epoch_for(frame)
        if epoch is None:
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

    async def mark_connected(self, cluster_uid: str, agent_version: str) -> None:
        """The observed half of a cluster coming up. Never touches `Cluster.phase`."""
        moment = now()
        await self._sync_state(
            cluster_uid,
            {
                "reachable": True,
                "agent_version": agent_version or None,
                "connected_at": moment,
                "last_seen_at": moment,
                "last_error": None,
            },
        )

    async def mark_disconnected(self, cluster_uid: str, error: str | None = None) -> None:
        moment = now()
        await self._sync_state(
            cluster_uid,
            {"reachable": False, "disconnected_at": moment, "last_seen_at": moment, "last_error": error},
        )

    async def touch(self, cluster_uid: str) -> None:
        await self._sync_state(cluster_uid, {"last_seen_at": now()})

    async def _sync_state(self, cluster_uid: str, values: dict[str, Any]) -> None:
        async with self.sessions.begin() as db:
            cluster_id = await self._cluster_id(db, cluster_uid)
            await db.execute(statements.upsert_sync_state(cluster_id, values))


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
