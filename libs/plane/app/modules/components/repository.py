from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Cluster
from app.db.models import Component
from app.db.models import ComponentStatus
from app.core.lookup import at_most_one
from app.core.filters import ref_ids


class ComponentRepository:
    """Reads the declared component and its observed status as one row.

    The two are joined, not lazy-loaded, because every question worth
    asking about a component (is it behind, is it healthy) is a question about
    the pair, and a `selectinload` per row to answer it is a second query for
    data the same join already has.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Component, ComponentStatus, Cluster.uid.label("cluster_uid"))
            .join(Cluster, Component.cluster_id == Cluster.id)
            .outerjoin(ComponentStatus, ComponentStatus.component_id == Component.id)
        )

    def get_query(self, ref: str, cluster: str | None = None) -> Select:
        column = Component.uid if is_uid(ref) else Component.name
        query = self._base().where(column == ref)
        if cluster is not None:
            query = query.where(Component.cluster_id.in_(ref_ids(Cluster, cluster)))
        return query

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        name: str | None = None,
        kind: str | None = None,
        behind: bool | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Select:
        query = self._base().order_by(Component.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Component.seq > after_seq)
        if cluster is not None:
            query = query.where(Component.cluster_id.in_(ref_ids(Cluster, cluster)))
        if name is not None:
            query = query.where(Component.name == name)
        if kind is not None:
            query = query.where(Component.kind == kind)
        if enabled is not None:
            query = query.where(Component.enabled.is_(enabled))
        if search is not None:
            query = query.where(Component.name.ilike(f"%{search}%"))
        if behind is not None:
            query = query.where(self._behind() if behind else ~self._behind())
        return query

    def _behind(self):
        """The predicate behind "which clusters are behind on this component".

        A component nobody has reported on yet counts as behind: a null observed
        version is not evidence that the declared one is running, and treating
        "unknown" as "current" is how a stalled rollout stays invisible.
        """
        return or_(
            ComponentStatus.observed_version.is_(None),
            ComponentStatus.observed_version != Component.desired_version,
        )

    async def get(self, ref: str, cluster: str | None = None, lock: bool = False) -> Row | None:
        """The one component `ref` names; a name two clusters share is refused unless `cluster` narrows it.

        `lock` holds the component row until the transaction ends, so an If-Match check
        and the write it guards see the same revision.
        """
        query = self.get_query(ref, cluster).limit(2)
        if lock:
            query = query.with_for_update(of=Component).execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return at_most_one(list(result.all()), "component", ref, "use its id or name a cluster")

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(select(Cluster).where(column == ref))
        return result.scalar_one_or_none()

    async def status(self, component_id) -> ComponentStatus | None:
        result = await self.db.execute(select(ComponentStatus).where(ComponentStatus.component_id == component_id))
        return result.scalar_one_or_none()

    async def add(self, component: Component) -> Component:
        self.db.add(component)
        await self.db.flush()
        return component

    async def delete(self, component: Component) -> None:
        await self.db.delete(component)
        await self.db.flush()
