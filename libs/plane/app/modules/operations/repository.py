import datetime as dt

from typing import NamedTuple

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import null
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import NODE
from app.core.ids import DEVICE
from app.core.ids import CLUSTER
from app.core.ids import ARTIFACT
from app.core.ids import WORKLOAD
from app.core.ids import COMPONENT
from app.core.ids import OPERATION
from app.core.ids import DEPLOYMENT
from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Artifact
from app.db.models import Workload
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
    WORKLOAD: Workload,
    COMPONENT: Component,
    ARTIFACT: Artifact,
}

TARGET_KINDS = {
    CLUSTER: "cluster",
    NODE: "node",
    DEVICE: "device",
    DEPLOYMENT: "deployment",
    WORKLOAD: "workload",
    COMPONENT: "component",
    ARTIFACT: "artifact",
}

TARGET_PREFIXES = {kind: prefix for prefix, kind in TARGET_KINDS.items()}

TARGET_NAME_COLUMNS = {
    CLUSTER: "name",
    NODE: "name",
    DEVICE: "device_uuid",
    DEPLOYMENT: "name",
    WORKLOAD: "name",
    COMPONENT: "name",
    ARTIFACT: "name",
}
"""The column a `kind:name` reference matches for each kind.

A device has no name of its own; the thing an operator reads off a machine is
its `device_uuid`, which is what `/v1/devices/{ref}` already resolves against.
"""


class TargetRef(NamedTuple):
    """A parsed target reference: which kind it names, and how to find it."""

    prefix: str
    model: type
    column: InstrumentedAttribute
    value: str


def parse_target(ref: str) -> TargetRef:
    """Reads a target reference as either a prefixed id or a `kind:name` pair.

    The kind has to be answerable at creation rather than three hops downstream
    in the operator, which is what a `node.drain` pointed at a `dev_...` is
    caught by. An id carries its kind in the prefix; a human types a name, which
    carries nothing, so the kind is written beside it — `node:gpu-node-04`. A
    bare name is refused rather than guessed at, because guessing is how a
    device and a deployment that happen to share a name become the same request.
    """
    kind, sep, name = ref.partition(":")
    if sep:
        prefix = TARGET_PREFIXES.get(kind)
        if prefix is None:
            raise Invalid(
                f"target {ref!r} names no kind operations act upon",
                {"target": ref, "allowed": sorted(TARGET_PREFIXES)},
            )
        if not name:
            raise Invalid(f"target {ref!r} has no name after {kind!r}", {"target": ref})
        model = TARGET_TABLES[prefix]
        return TargetRef(prefix, model, getattr(model, TARGET_NAME_COLUMNS[prefix]), name)
    if not is_uid(ref):
        raise Invalid(
            f"target {ref!r} is neither an identifier nor a kind:name reference",
            {"target": ref, "forms": ["nod_01hq0v...", "node:gpu-node-04"]},
        )
    model = TARGET_TABLES.get(ref[:4])
    if model is None:
        raise Invalid(f"target {ref!r} is not a kind operations act upon", {"target": ref})
    return TargetRef(ref[:4], model, model.uid, ref)


def target_model(ref: str):
    """The table a target reference names, or `Invalid` if it names none."""
    return parse_target(ref).model


def target_kind(ref: str) -> str:
    """The `target_type` a target reference implies.

    Filtering on `target_id` alone would compare primary keys across six tables,
    so the kind is matched too, and it comes from the prefix or from the
    `kind:` half of a qualified reference.
    """
    return TARGET_KINDS[parse_target(ref).prefix]


def target_subquery(ref: str) -> Select:
    """The row ids `Operation.target_id` may hold for a target reference.

    A set rather than a single id: a name is unique per cluster and not beyond
    it, and a filter that matches every node called `gpu-node-04` is a filter
    doing what it says. Creation is the path that refuses an ambiguous name.
    """
    parsed = parse_target(ref)
    return select(parsed.model.id).where(parsed.column == parsed.value)


class OperationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Operation, Cluster.uid.label("cluster_uid")).outerjoin(
            Cluster, Operation.cluster_id == Cluster.id
        )

    def get_query(self, ref: str) -> Select:
        """Operations are addressed by id only.

        `libs/packages/contract/API.md` §3 lets a path segment be an id *or* a name where the
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
        parsed = parse_target(ref)
        model = parsed.model
        if model is Cluster:
            cluster_column = model.id.label("cluster_id")
        elif hasattr(model, "cluster_id"):
            cluster_column = model.cluster_id
        else:
            cluster_column = null().label("cluster_id")
        return select(model.id, model.uid, cluster_column).where(parsed.column == parsed.value)

    async def resolve_targets(self, ref: str) -> "list[Row]":
        """Every row a target reference matches, so creation can answer for all of them.

        A missing target is a 404 at creation and a name matching twice is a 400;
        neither is a decision the repository makes, so both counts come back.
        """
        result = await self.db.execute(self.target_query(ref))
        return list(result.all())

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
