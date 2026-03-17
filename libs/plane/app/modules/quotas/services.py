from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import QUOTA
from app.core.ids import new_uid
from app.db.models import Quota
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import QuotaSchema
from .schemas import QuotaUsageSchema
from .schemas import QuotaCreateRequest
from .schemas import QuotaUpdateRequest
from .repository import QuotaRepository


EMPTY = QuotaUsageSchema(allocated_devices=0, live_allocations=0, over_quota_devices=0)


def _revision(quota: Quota) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = quota.updated_at or quota.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def usage_for(quota: Quota, rollup: dict) -> QuotaUsageSchema:
    """Folds the per-(project, cluster, class) rollup down to one quota's scope.

    A quota with no cluster spans every cluster, so its usage is the sum of all
    the cluster-keyed rows for its project and device class rather than the row
    whose cluster is null — no allocation is ever recorded without a cluster, so
    reading a null-cluster quota as a null-cluster lookup would always report
    zero usage against a limit that is really being consumed.
    """
    devices = allocations = over_quota = 0
    for (project_id, cluster_id, device_class), row in rollup.items():
        if project_id != quota.project_id or device_class != quota.device_class:
            continue
        if quota.cluster_id is not None and cluster_id != quota.cluster_id:
            continue
        devices += row["devices"]
        allocations += row["allocations"]
        over_quota += row["over_quota_devices"]
    return QuotaUsageSchema(
        allocated_devices=devices,
        live_allocations=allocations,
        over_quota_devices=over_quota,
    )


def headroom(limit_devices: int | None, allocated: int) -> int | None:
    """Devices still available under the hard limit. Null when there is no limit."""
    if limit_devices is None:
        return None
    return limit_devices - allocated


def over_limit(limit_devices: int | None, allocated: int) -> bool:
    if limit_devices is None:
        return False
    return allocated > limit_devices


def validate_numbers(guaranteed: int, limit: int | None, weight: float) -> None:
    """Rejects a quota that cannot mean anything.

    A limit below the guarantee is the interesting case: the two numbers are a
    floor and a ceiling, and a ceiling under the floor promises capacity in one
    field and forbids it in the other.
    """
    if guaranteed < 0:
        raise Invalid("guaranteed_devices cannot be negative", {"guaranteed_devices": guaranteed})
    if limit is not None and limit < 0:
        raise Invalid("limit_devices cannot be negative", {"limit_devices": limit})
    if limit is not None and limit < guaranteed:
        raise Invalid(
            "limit_devices is below guaranteed_devices",
            {"guaranteed_devices": guaranteed, "limit_devices": limit},
        )
    if weight < 0:
        raise Invalid("over_quota_weight cannot be negative", {"over_quota_weight": weight})


class QuotaService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = QuotaRepository(db)

    def schema(self, row: Row, usage: QuotaUsageSchema) -> QuotaSchema:
        quota: Quota = row[0]
        return QuotaSchema(
            id=quota.uid,
            project=row.project_uid,
            project_slug=row.project_slug,
            cluster=row.cluster_uid,
            cluster_name=row.cluster_name,
            device_class=quota.device_class,
            guaranteed_devices=quota.guaranteed_devices,
            limit_devices=quota.limit_devices,
            over_quota_weight=float(quota.over_quota_weight),
            over_quota_enabled=quota.over_quota_enabled,
            created_at=quota.created_at,
            updated_at=quota.updated_at,
            revision=_revision(quota),
            usage=usage,
            headroom_devices=headroom(quota.limit_devices, usage.allocated_devices),
            over_limit=over_limit(quota.limit_devices, usage.allocated_devices),
        )

    async def _one(self, ref: str) -> Row:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no quota {ref!r}")
        return row

    async def _with_usage(self, rows: list[Row]) -> list[QuotaSchema]:
        rollup = await self.repo.usage([r[0].project_id for r in rows])
        return [self.schema(r, usage_for(r[0], rollup)) for r in rows]

    async def get(self, ref: str) -> QuotaSchema:
        row = await self._one(ref)
        return (await self._with_usage([row]))[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        cluster: str | None = None,
        device_class: str | None = None,
        over_quota_enabled: bool | None = None,
    ) -> ListResponse[QuotaSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            cluster=cluster,
            device_class=device_class,
            over_quota_enabled=over_quota_enabled,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = await self._with_usage(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: QuotaCreateRequest) -> QuotaSchema:
        validate_numbers(data.guaranteed_devices, data.limit_devices, data.over_quota_weight)
        if not data.device_class:
            raise Invalid("device_class is required")
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        cluster = None
        if data.cluster is not None:
            cluster = await self.repo.cluster_by_ref(data.cluster)
            if cluster is None:
                raise NotFound(f"no cluster {data.cluster!r}")
        quota = Quota(
            uid=new_uid(QUOTA),
            project_id=project.id,
            cluster_id=cluster.id if cluster else None,
            device_class=data.device_class,
            guaranteed_devices=data.guaranteed_devices,
            limit_devices=data.limit_devices,
            over_quota_weight=data.over_quota_weight,
            over_quota_enabled=data.over_quota_enabled,
        )
        try:
            await self.repo.add(quota)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(
                f"a quota for {data.device_class!r} in {data.project!r} already exists at that scope"
            ) from exc
        return await self.get(quota.uid)

    async def update(self, ref: str, data: QuotaUpdateRequest, if_match: str | None = None) -> QuotaSchema:
        row = await self._one(ref)
        quota: Quota = row[0]
        if if_match is not None and if_match != _revision(quota):
            raise Conflict("quota changed since it was read", {"revision": _revision(quota)})

        fields = data.to_dict(exclude_unset=True)
        validate_numbers(
            fields.get("guaranteed_devices", quota.guaranteed_devices),
            fields.get("limit_devices", quota.limit_devices),
            fields.get("over_quota_weight", float(quota.over_quota_weight)),
        )
        for key, value in fields.items():
            setattr(quota, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("quota conflicts with an existing one") from exc
        return await self.get(quota.uid)

    async def delete(self, ref: str) -> None:
        row = await self._one(ref)
        await self.repo.delete(row[0])
        await self.db.commit()
