from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.supply import device_free
from app.db.supply import device_held
from app.core.filters import ref_ids


class DeviceClassRepository:
    """Reads only, and reads one aggregate.

    The catalog itself is a file, so there is no table behind this module and
    nothing here writes. What it does read is the fleet's inventory of each
    class, as one grouped query instead of one query per class: the table is
    small today and would still be N round trips.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def query(self, cluster: str | None = None) -> Select:
        """Device counts per class, grouped, with the clusters each is present in."""
        query = (
            select(
                Device.device_class,
                func.count(Device.id),
                func.count(case((Device.health == "healthy", 1))),
                func.count(case((device_free(), 1))),
                func.count(case((device_held(), 1))),
                func.count(func.distinct(Device.node_id)),
                func.array_agg(func.distinct(Cluster.name)),
            )
            .join(Cluster, Device.cluster_id == Cluster.id)
            .join(Node, Device.node_id == Node.id)
            .group_by(Device.device_class)
        )
        if cluster is not None:
            query = query.where(Device.cluster_id.in_(ref_ids(Cluster, cluster)))
        return query

    async def inventory(self, cluster: str | None = None) -> list[Row]:
        result = await self.db.execute(self.query(cluster=cluster))
        return list(result.all())
