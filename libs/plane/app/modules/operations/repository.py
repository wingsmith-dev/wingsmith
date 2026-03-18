import datetime as dt

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import null
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import NODE
from app.core.ids import DEVICE
from app.core.ids import CLUSTER
from app.core.ids import ARTIFACT
from app.core.ids import COMPONENT
from app.core.ids import OPERATION
from app.core.ids import DEPLOYMENT
from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Component
from app.db.models import Operation
from app.db.models import Deployment
from app.core.filters import ref_ids
from app.core.exceptions import Invalid


TARGET_TABLES = {
    CLUSTER: Cluster,
    NODE: Node,
    DEVICE: Device,
    DEPLOYMENT: Deployment,
    COMPONENT: Component,
    ARTIFACT: Artifact,
}

TARGET_KINDS = {
    CLUSTER: "cluster",
    NODE: "node",
    DEVICE: "device",
    DEPLOYMENT: "deployment",
    COMPONENT: "component",
    ARTIFACT: "artifact",
}


def target_model(ref: str):
    """The table a target identifier names, or `Invalid` if it names none.

    The prefix is the only thing a target reference carries about its own kind,
    which is exactly what makes `node.drain` pointed at a `dev_...` answerable at
    creation rather than three hops downstream in the operator.
    """
    if not is_uid(ref):
        raise Invalid(f"target {ref!r} is not an identifier", {"target": ref})
    model = TARGET_TABLES.get(ref[:4])
    if model is None:
        raise Invalid(f"target {ref!r} is not a kind operations act upon", {"target": ref})
    return model


def target_kind(ref: str) -> str:
    """The `target_type` a target identifier implies.

    Filtering on `target_id` alone would compare primary keys across six tables,
    so the kind is matched too and the prefix is where it comes from.
    """
    target_model(ref)
    return TARGET_KINDS[ref[:4]]


def target_subquery(ref: str) -> Select:
    """The row id `Operation.target_id` holds for a public target id."""
    model = target_model(ref)
    return select(model.id).where(model.uid == ref)


class OperationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Operation, Cluster.uid.label("cluster_uid")).outerjoin(
            Cluster, Operation.cluster_id == Cluster.id
        )

    def get_query(self, ref: str) -> Select:
        """Operations are addressed by id only.

        `contract/API.md` §3 lets a path segment be an id *or* a name where the
        object has a unique name. An operation has none — "the drain" is not a
        thing anybody can name twice — so there is nothing here to resolve
        against and the id is the only reference.
        """
        return self._base().where(Operation.uid == ref)

    def list_query(
        self,
        limit: int,
        before_seq: int | None = None,
        cluster: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        target: str | None = None,
        actor: str | None = None,
        since: dt.datetime | None = None,
    ) -> Select:
        """Newest first: an operations list is read to see what is happening now."""
        query = self._base().order_by(Operation.seq.desc()).limit(limit + 1)
        if before_seq is not None:
            query = query.where(Operation.seq < before_seq)
        if cluster is not None:
            query = query.where(Operation.cluster_id.in_(ref_ids(Cluster, cluster)))
        if kind is not None:
            query = query.where(Operation.type == kind)
        if phase is not None:
            query = query.where(Operation.phase == phase)
        if target is not None:
            query = query.where(
                Operation.target_type == target_kind(target),
                Operation.target_id.in_(target_subquery(target)),
            )
        if actor is not None:
            query = query.where(Operation.requested_by == actor)
        if since is not None:
            query = query.where(Operation.created_at >= since)
        return query

    def idempotency_query(self, key: str) -> Select:
        """Finds a prior operation created under the same `Idempotency-Key`.

        `idempotency_key` is a column with a partial unique index behind it, so
        this lookup is the fast path and the index is the correctness: two
        retries arriving at once can both miss here, and the second one loses at
        insert rather than becoming a second drain.
        """
        return self._base().where(Operation.idempotency_key == key)

    async def get(self, ref: str) -> Row | None:
        if not is_uid(ref) or not ref.startswith(OPERATION):
            return None
        result = await self.db.execute(self.get_query(ref))
        return result.first()

    async def by_idempotency_key(self, key: str) -> Row | None:
        result = await self.db.execute(self.idempotency_query(key).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    def target_query(self, ref: str) -> Select:
        """Loads the target row and, where it has one, the cluster it belongs to.

        A cluster is its own cluster and an artifact belongs to none — artifacts
        are global, which is why `cache.warm` is the one type that has to be told
        which cluster to warm.
        """
        model = target_model(ref)
        if model is Cluster:
            cluster_column = model.id.label("cluster_id")
        elif hasattr(model, "cluster_id"):
            cluster_column = model.cluster_id
        else:
            cluster_column = null().label("cluster_id")
        return select(model.id, model.uid, cluster_column).where(model.uid == ref)

    async def resolve_target(self, ref: str) -> Row | None:
        """Loads the target row itself, so that a missing target is a 404 at creation."""
        result = await self.db.execute(self.target_query(ref))
        return result.first()

    async def target_uid(self, prefix: str, row_id) -> str | None:
        """The public id of a target, given the table its `target_type` names."""
        model = TARGET_TABLES.get(prefix)
        if model is None:
            return None
        result = await self.db.execute(select(model.uid).where(model.id == row_id))
        return result.scalar_one_or_none()

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(select(Cluster).where(column == ref))
        return result.scalar_one_or_none()

    async def cluster_uid(self, cluster_id) -> str | None:
        if cluster_id is None:
            return None
        result = await self.db.execute(select(Cluster.uid).where(Cluster.id == cluster_id))
        return result.scalar_one_or_none()

    async def add(self, operation: Operation) -> Operation:
        self.db.add(operation)
        await self.db.flush()
        return operation
