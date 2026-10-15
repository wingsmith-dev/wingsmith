from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Policy
from app.db.models import Tenant
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import PolicyAttachment
from app.core.filters import ref_ids


SUBJECTS = {
    "tenant": (Tenant, "slug", "uid"),
    "project": (Project, "slug", "uid"),
    "cluster": (Cluster, "name", "uid"),
    "endpoint": (Endpoint, "name", "uid"),
    "workload": (Workload, "name", "uid"),
}


def ref_column(ref: str):
    """A policy is addressed by its `plc_` id or by its unique name."""
    return Policy.uid if is_uid(ref) else Policy.name


class PolicyRepository:
    """Reads policies, and their attachments in one grouped query.

    `Policy.attachments` is `lazy="noload"`, and a list of policies would emit
    one round trip per row if the serializer walked it, so attachments are
    fetched for the whole page at once and keyed by policy.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def get_query(self, ref: str) -> Select:
        return select(Policy).where(ref_column(ref) == ref)

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        enabled: bool | None = None,
        region: str | None = None,
        device_class: str | None = None,
        search: str | None = None,
        subject_ids: Select | None = None,
    ) -> Select:
        query = select(Policy).order_by(Policy.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Policy.seq > after_seq)
        if enabled is not None:
            query = query.where(Policy.enabled.is_(enabled))
        if region is not None:
            query = query.where(Policy.allowed_regions.any(region))
        if device_class is not None:
            query = query.where(Policy.allowed_device_classes.any(device_class))
        if search is not None:
            query = query.where(Policy.name.ilike(f"%{search}%"))
        if subject_ids is not None:
            query = query.where(Policy.id.in_(subject_ids))
        return query

    def attachment_query(self, policy_ids: list) -> Select:
        return (
            select(PolicyAttachment)
            .where(PolicyAttachment.policy_id.in_(policy_ids))
            .order_by(PolicyAttachment.seq.asc())
        )

    def policy_ids_for_subject(self, subject_type: str, subject_id: UUID) -> Select:
        return select(PolicyAttachment.policy_id).where(
            PolicyAttachment.subject_type == subject_type,
            PolicyAttachment.subject_id == subject_id,
        )

    def subject_query(self, subject_type: str, ref: str) -> Select | None:
        """Resolves a subject reference within its own type's namespace.

        The reference is an id or a name exactly as it is everywhere else; the
        type has to come with it because `subject_id` is polymorphic and the same
        name can exist under two subject types.
        """
        entry = SUBJECTS.get(subject_type)
        if entry is None:
            return None
        model, name_column, _ = entry
        column = model.uid if is_uid(ref) else getattr(model, name_column)
        return select(model).where(column == ref).limit(1)

    def subjects_query(self, subject_type: str, ids: list) -> Select | None:
        entry = SUBJECTS.get(subject_type)
        if entry is None:
            return None
        model = entry[0]
        return select(model).where(model.id.in_(ids))

    async def get(self, ref: str, lock: bool = False) -> Policy | None:
        """`lock` holds the row until the transaction ends, re-read, so an `If-Match` compared under it holds."""
        query = self.get_query(ref).limit(1)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def list(self, **kwargs) -> list[Policy]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.scalars())

    async def attachments(self, policy_ids: list) -> dict:
        grouped: dict = {}
        if not policy_ids:
            return grouped
        result = await self.db.execute(self.attachment_query(policy_ids))
        for row in result.scalars():
            grouped.setdefault(row.policy_id, []).append(row)
        return grouped

    async def subject(self, subject_type: str, ref: str):
        query = self.subject_query(subject_type, ref)
        if query is None:
            return None
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def subjects(self, subject_type: str, ids: list) -> dict:
        """Public references for a batch of subjects of one type."""
        query = self.subjects_query(subject_type, ids)
        if query is None or not ids:
            return {}
        _, name_column, public = SUBJECTS[subject_type]
        result = await self.db.execute(query)
        return {row.id: (getattr(row, public), getattr(row, name_column)) for row in result.scalars()}

    async def cluster_by_ref(self, ref: str) -> Cluster | None:
        """A policy's cluster lists are raw keys; the API speaks ids and names."""
        result = await self.db.execute(select(Cluster).where(Cluster.id.in_(ref_ids(Cluster, ref))))
        return result.scalar_one_or_none()

    async def cluster_uids(self, ids: list) -> dict:
        if not ids:
            return {}
        result = await self.db.execute(select(Cluster.id, Cluster.uid).where(Cluster.id.in_(ids)))
        return {row.id: row.uid for row in result.all()}

    async def add(self, policy: Policy) -> Policy:
        self.db.add(policy)
        await self.db.flush()
        return policy

    async def add_attachment(self, attachment: PolicyAttachment) -> PolicyAttachment:
        self.db.add(attachment)
        await self.db.flush()
        return attachment

    async def drop_attachments(self, policy_id: UUID) -> None:
        result = await self.db.execute(self.attachment_query([policy_id]))
        for row in result.scalars():
            await self.db.delete(row)
        await self.db.flush()

    async def delete(self, policy: Policy) -> None:
        await self.db.delete(policy)
        await self.db.flush()
