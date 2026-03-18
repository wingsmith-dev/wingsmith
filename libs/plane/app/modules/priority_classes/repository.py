from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Workload
from app.db.models import PriorityClass


def ref_column(ref: str):
    """A priority class is addressed by its `prc_` id or by its unique name."""
    return PriorityClass.uid if is_uid(ref) else PriorityClass.name


class PriorityClassRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def get_query(self, ref: str) -> Select:
        return select(PriorityClass).where(ref_column(ref) == ref)

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        preemptible: bool | None = None,
        preempts: bool | None = None,
        search: str | None = None,
    ) -> Select:
        query = select(PriorityClass).order_by(PriorityClass.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(PriorityClass.seq > after_seq)
        if preemptible is not None:
            query = query.where(PriorityClass.preemptible.is_(preemptible))
        if preempts is not None:
            query = query.where(PriorityClass.preempts.is_(preempts))
        if search is not None:
            query = query.where(PriorityClass.name.ilike(f"%{search}%"))
        return query

    def value_holder_query(self, value: int, exclude: UUID | None = None) -> Select:
        """Whoever already owns a priority value. Two owners is an undefined order."""
        query = select(PriorityClass).where(PriorityClass.value == value)
        if exclude is not None:
            query = query.where(PriorityClass.id != exclude)
        return query

    def workload_count_query(self, priority_class_id: UUID) -> Select:
        return select(func.count(Workload.id)).where(Workload.priority_class_id == priority_class_id)

    async def get(self, ref: str) -> PriorityClass | None:
        result = await self.db.execute(self.get_query(ref))
        return result.scalar_one_or_none()

    async def list(self, **kwargs) -> list[PriorityClass]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.scalars())

    async def value_holder(self, value: int, exclude: UUID | None = None) -> PriorityClass | None:
        result = await self.db.execute(self.value_holder_query(value, exclude).limit(1))
        return result.scalar_one_or_none()

    async def workload_count(self, priority_class_id: UUID) -> int:
        result = await self.db.execute(self.workload_count_query(priority_class_id))
        return int(result.scalar_one())

    async def clear_default(self, keep: UUID | None = None) -> None:
        query = select(PriorityClass).where(PriorityClass.is_default.is_(True))
        if keep is not None:
            query = query.where(PriorityClass.id != keep)
        result = await self.db.execute(query)
        for row in result.scalars():
            row.is_default = False
        await self.db.flush()

    async def add(self, priority_class: PriorityClass) -> PriorityClass:
        self.db.add(priority_class)
        await self.db.flush()
        return priority_class

    async def delete(self, priority_class: PriorityClass) -> None:
        await self.db.delete(priority_class)
        await self.db.flush()
