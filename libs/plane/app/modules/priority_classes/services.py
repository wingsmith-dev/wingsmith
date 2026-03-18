from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import PRIORITY_CLASS
from app.core.ids import new_uid
from app.db.models import PriorityClass
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import PriorityClassSchema
from .schemas import PriorityClassCreateRequest
from .schemas import PriorityClassUpdateRequest
from .repository import PriorityClassRepository


def _revision(priority_class: PriorityClass) -> str:
    stamp = priority_class.updated_at or priority_class.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def precedence(classes: list[PriorityClass]) -> list[PriorityClass]:
    """Highest value first, name as the tiebreaker.

    The tiebreaker is vestigial now that `value` is unique in the database, and
    is kept because a total order that does not depend on the constraint holding
    costs one extra sort key.
    """
    return sorted(classes, key=lambda c: (-c.value, c.name))


class PriorityClassService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PriorityClassRepository(db)

    def schema(self, priority_class: PriorityClass) -> PriorityClassSchema:
        return PriorityClassSchema(
            id=priority_class.uid,
            name=priority_class.name,
            value=priority_class.value,
            preemptible=priority_class.preemptible,
            preempts=priority_class.preempts,
            is_default=priority_class.is_default,
            description=priority_class.description,
            created_at=priority_class.created_at,
            updated_at=priority_class.updated_at,
            revision=_revision(priority_class),
        )

    async def _one(self, ref: str) -> PriorityClass:
        found = await self.repo.get(ref)
        if found is None:
            raise NotFound(f"no priority class {ref!r}")
        return found

    async def get(self, ref: str) -> PriorityClassSchema:
        return self.schema(await self._one(ref))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        preemptible: bool | None = None,
        preempts: bool | None = None,
        search: str | None = None,
    ) -> ListResponse[PriorityClassSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            preemptible=preemptible,
            preempts=preempts,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda c: c.seq)
        items = [self.schema(c) for c in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def _claim_value(self, value: int, exclude=None) -> None:
        holder = await self.repo.value_holder(value, exclude)
        if holder is not None:
            raise Conflict(
                f"priority value {value} is already held by {holder.name!r}",
                {"value": value, "held_by": holder.name},
            )

    async def create(self, data: PriorityClassCreateRequest) -> PriorityClassSchema:
        if not data.name:
            raise Invalid("name is required")
        await self._claim_value(data.value)
        priority_class = PriorityClass(
            uid=new_uid(PRIORITY_CLASS),
            name=data.name,
            value=data.value,
            preemptible=data.preemptible,
            preempts=data.preempts,
            is_default=data.is_default,
            description=data.description,
        )
        try:
            await self.repo.add(priority_class)
            if data.is_default:
                await self.repo.clear_default(keep=priority_class.id)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"priority class {data.name!r} already exists") from exc
        return await self.get(priority_class.uid)

    async def update(
        self, ref: str, data: PriorityClassUpdateRequest, if_match: str | None = None
    ) -> PriorityClassSchema:
        priority_class = await self._one(ref)
        if if_match is not None and if_match != _revision(priority_class):
            raise Conflict("priority class changed since it was read", {"revision": _revision(priority_class)})

        fields = data.to_dict(exclude_unset=True)
        if "value" in fields:
            await self._claim_value(fields["value"], exclude=priority_class.id)
        for key, value in fields.items():
            setattr(priority_class, key, value)

        try:
            if fields.get("is_default"):
                await self.repo.clear_default(keep=priority_class.id)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("priority class conflicts with an existing one") from exc
        return await self.get(priority_class.uid)

    async def delete(self, ref: str) -> None:
        """Refuses while workloads still reference the class.

        `Workload.priority_class_id` is `ON DELETE RESTRICT` and not nullable, so
        the alternative to refusing is an integrity error at commit. Checking
        first turns it into the conflict the contract describes, with a count the
        caller can act on.
        """
        priority_class = await self._one(ref)
        workloads = await self.repo.workload_count(priority_class.id)
        if workloads:
            raise Conflict(
                f"priority class {ref!r} is referenced by {workloads} workload(s)",
                {"workloads": workloads},
            )
        await self.repo.delete(priority_class)
        await self.db.commit()
