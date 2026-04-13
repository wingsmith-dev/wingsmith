import datetime as dt

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.db.models import Cluster
from app.db.models import ClusterSyncState
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ClusterSchema
from .schemas import ClusterSyncSchema
from .schemas import ClusterCreateRequest
from .schemas import ClusterUpdateRequest
from .repository import ClusterRepository


PHASES = frozenset({"pending", "active", "draining", "disabled"})


def _revision(cluster: Cluster) -> str:
    """The value `If-Match` is checked against.

    Derived from the last write rather than stored, because `updated_at` already
    moves on every update and a second column tracking the same fact is a second
    column that can disagree with it.
    """
    stamp = cluster.updated_at or cluster.created_at
    return str(int(stamp.timestamp() * 1_000_000))


STALE_AFTER = dt.timedelta(minutes=10)
"""How long a connected cluster may go without sending anything before its link
is reported `stale` rather than `connected`.

Long enough to sit well clear of the operator's own observe and resync intervals,
short enough that nobody reads two-day-old inventory as current. The failure this
exists for is an operator whose observation loop stopped while its socket stayed
up: `reachable` is true, because the socket is the only thing it ever measured,
and every number the console draws from that cluster is frozen with nothing
saying so."""


def _sync(state: ClusterSyncState | None) -> ClusterSyncSchema:
    if state is None or state.connected_at is None:
        return ClusterSyncSchema(
            state="never",
            last_sync_at=None,
            last_seen_at=None,
            agent_version=state.agent_version if state else None,
            lag_ms=None,
            last_error=state.last_error if state else None,
            distribution=state.distribution if state else None,
            cloud=state.cloud if state else None,
            observed_region=state.observed_region if state else None,
        )
    lag = None
    stale = False
    if state.last_sync_at is not None:
        now = dt.datetime.now(dt.UTC)
        since = now - state.last_sync_at
        lag = max(0, int(since.total_seconds() * 1000))
        stale = since > STALE_AFTER

    if not state.reachable:
        link = "disconnected"
    elif stale:
        link = "stale"
    else:
        link = "connected"

    return ClusterSyncSchema(
        state=link,
        last_sync_at=state.last_sync_at,
        last_seen_at=state.last_seen_at,
        agent_version=state.agent_version,
        lag_ms=lag,
        last_error=state.last_error,
        distribution=state.distribution,
        cloud=state.cloud,
        observed_region=state.observed_region,
    )


class ClusterService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClusterRepository(db)

    def schema(self, cluster: Cluster) -> ClusterSchema:
        state = cluster.sync_state
        return ClusterSchema(
            id=cluster.uid,
            name=cluster.name,
            display_name=cluster.display_name,
            region=cluster.region,
            zone=cluster.zone,
            residency=cluster.residency,
            phase=cluster.phase,
            labels=cluster.labels,
            capabilities=cluster.capabilities,
            enrolled_at=cluster.enrolled_at,
            created_at=cluster.created_at,
            updated_at=cluster.updated_at,
            revision=_revision(cluster),
            reachable=bool(state and state.reachable),
            sync=_sync(state),
        )

    async def get(self, ref: str) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        return self.schema(cluster)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        region: str | None = None,
        phase: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[ClusterSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            region=region,
            phase=phase,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda c: c.seq)
        items = [self.schema(c) for c in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ClusterCreateRequest) -> ClusterSchema:
        if data.phase not in PHASES:
            raise Invalid(f"unknown phase {data.phase!r}", {"allowed": sorted(PHASES)})
        cluster = Cluster(
            uid=new_uid(CLUSTER),
            name=data.name,
            display_name=data.display_name,
            region=data.region,
            zone=data.zone,
            residency=data.residency,
            phase=data.phase,
            labels=data.labels,
            capabilities=data.capabilities,
        )
        try:
            await self.repo.add(cluster)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"cluster {data.name!r} already exists") from exc
        return await self.get(cluster.uid)

    async def update(self, ref: str, data: ClusterUpdateRequest, if_match: str | None = None) -> ClusterSchema:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        if if_match is not None and if_match != _revision(cluster):
            raise Conflict("cluster changed since it was read", {"revision": _revision(cluster)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "phase" in fields and fields["phase"] not in PHASES:
            raise Invalid(f"unknown phase {fields['phase']!r}", {"allowed": sorted(PHASES)})
        for key, value in fields.items():
            setattr(cluster, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("cluster conflicts with an existing one") from exc
        return await self.get(cluster.uid)

    async def delete(self, ref: str) -> None:
        cluster = await self.repo.get(ref)
        if cluster is None:
            raise NotFound(f"no cluster {ref!r}")
        await self.repo.delete(cluster)
        await self.db.commit()
