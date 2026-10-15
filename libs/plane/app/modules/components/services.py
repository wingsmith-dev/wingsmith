from msgspec import UNSET
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import COMPONENT
from app.core.ids import new_uid
from app.db.models import Component
from app.db.models import ComponentStatus
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ComponentSchema
from .schemas import ComponentStatusSchema
from .schemas import ComponentCreateRequest
from .schemas import ComponentUpdateRequest
from .repository import ComponentRepository


UNREPORTED = ComponentStatusSchema(
    observed_version=None,
    rollout_phase="unknown",
    healthy=False,
    message=None,
    observed_at=None,
)
"""What a component looks like before any cluster has reported on it.

Deliberately not `null`: a client that has to branch on the absence of the
observed half will forget to, and "never reported" and "reported unhealthy" are
both answers to the same question.
"""


def _revision(component: Component) -> str:
    """The value `If-Match` is checked against, derived from the last write."""
    stamp = component.updated_at or component.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _status(status: ComponentStatus | None) -> ComponentStatusSchema:
    if status is None:
        return UNREPORTED
    return ComponentStatusSchema(
        observed_version=status.observed_version,
        rollout_phase=status.rollout_phase,
        healthy=status.healthy,
        message=status.message,
        observed_at=status.observed_at,
    )


def is_behind(desired_version: str, observed_version: str | None) -> bool:
    """Whether a cluster is not yet running what it was told to run.

    A string comparison and not a version comparison: `desired_version` is an
    opaque tag, and ordering tags would mean guessing at a versioning scheme the
    component never promised to follow. Unreported counts as behind.
    """
    return observed_version is None or observed_version != desired_version


class ComponentService:
    """Full CRUD over the declared half, and nothing at all over the observed one.

    There is no method here that writes `ComponentStatus`. Observed state has
    exactly one writer and it is ingestion; a service method that also wrote it
    would be a second, and the two would diverge the first time a rollout was
    slow.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ComponentRepository(db)

    def schema(self, component: Component, cluster_uid: str, status: ComponentStatus | None) -> ComponentSchema:
        observed = _status(status)
        return ComponentSchema(
            id=component.uid,
            cluster_id=cluster_uid,
            name=component.name,
            kind=component.kind,
            desired_version=component.desired_version,
            config=component.config,
            enabled=component.enabled,
            created_at=component.created_at,
            updated_at=component.updated_at,
            revision=_revision(component),
            status=observed,
            behind=is_behind(component.desired_version, observed.observed_version),
        )

    def _row_schema(self, row: Row) -> ComponentSchema:
        return self.schema(row[0], row.cluster_uid, row[1])

    async def _one(self, ref: str, cluster: str | None = None, lock: bool = False) -> Row:
        row = await self.repo.get(ref, cluster, lock=lock)
        if row is None:
            raise NotFound(f"no component {ref!r}")
        return row

    async def get(self, ref: str, cluster: str | None = None) -> ComponentSchema:
        return self._row_schema(await self._one(ref, cluster))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        name: str | None = None,
        kind: str | None = None,
        behind: bool | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> ListResponse[ComponentSchema]:
        """`?name=agent&behind=true` is the question this resource exists to answer."""
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            name=name,
            kind=kind,
            behind=behind,
            enabled=enabled,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self._row_schema(row) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ComponentCreateRequest) -> ComponentSchema:
        if not data.name:
            raise Invalid("component name is required")
        if not data.desired_version:
            raise Invalid("desired_version is required", {"name": data.name})
        cluster = await self.repo.cluster_by_ref(data.cluster)
        if cluster is None:
            raise NotFound(f"no cluster {data.cluster!r}")
        component = Component(
            uid=new_uid(COMPONENT),
            cluster_id=cluster.id,
            name=data.name,
            kind=data.kind,
            desired_version=data.desired_version,
            config=data.config,
            enabled=data.enabled,
        )
        try:
            await self.repo.add(component)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"component {data.name!r} already exists in {data.cluster!r}") from exc
        return await self.get(component.uid)

    async def update(
        self, ref: str, data: ComponentUpdateRequest, if_match: str | None = None, cluster: str | None = None
    ) -> ComponentSchema:
        """Rewrites the declared version. The rollout that follows is the cluster's.

        Bumping `desired_version` does not touch `ComponentStatus`, so the
        component reads as `behind` from the moment it is asked for until the
        cluster says otherwise. That gap is the rollout, and it is meant to be
        visible.
        """
        row = await self._one(ref, cluster, lock=True)
        component = row[0]
        if if_match is not None and if_match != _revision(component):
            raise Conflict("component changed since it was read", {"revision": _revision(component)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "desired_version" in fields and not fields["desired_version"]:
            raise Invalid("desired_version cannot be empty")
        for key, value in fields.items():
            setattr(component, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("component conflicts with an existing one") from exc
        return await self.get(component.uid)

    async def delete(self, ref: str, cluster: str | None = None) -> None:
        """Deletes the declaration; the status row goes with it by cascade.

        Nothing is left behind reporting on something nobody declared, which is
        the same rule ingestion follows in the other direction when it refuses to
        create a status for an undeclared component.
        """
        row = await self._one(ref, cluster)
        await self.repo.delete(row[0])
        await self.db.commit()
