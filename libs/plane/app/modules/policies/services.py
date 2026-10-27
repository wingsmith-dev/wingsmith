from __future__ import annotations

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import POLICY
from app.core.ids import new_uid
from app.db.models import Policy
from app.db.models import PolicyAttachment
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import PolicySchema
from .schemas import PolicyCreateRequest
from .schemas import PolicyUpdateRequest
from .schemas import PolicyAttachmentSchema
from .schemas import PolicyAttachmentRequest
from .repository import SUBJECTS
from .repository import PolicyRepository


SUBJECT_TYPES = frozenset(SUBJECTS)


def _revision(policy: Policy) -> str:
    stamp = policy.updated_at or policy.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def check_cluster_lists(allowed: list[str], denied: list[str]) -> None:
    """A cluster on both lists is a policy that cannot be evaluated.

    Allow and deny are not opposite ends of one axis, an empty allow-list means
    "no opinion", so the only genuinely contradictory input is naming the same
    cluster twice, and it has to be refused, not resolved by precedence
    nobody wrote down.
    """
    overlap = sorted(set(allowed) & set(denied))
    if overlap:
        raise Invalid("a cluster cannot be both allowed and denied", {"clusters": overlap})


def check_subject_type(subject_type: str) -> None:
    if subject_type not in SUBJECT_TYPES:
        raise Invalid(f"unknown subject type {subject_type!r}", {"allowed": sorted(SUBJECT_TYPES)})


class PolicyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PolicyRepository(db)

    def schema(
        self,
        policy: Policy,
        attachments: list[PolicyAttachmentSchema],
        cluster_uids: dict,
    ) -> PolicySchema:
        return PolicySchema(
            id=policy.uid,
            name=policy.name,
            description=policy.description,
            allowed_regions=list(policy.allowed_regions),
            allowed_residencies=list(policy.allowed_residencies),
            allowed_device_classes=list(policy.allowed_device_classes),
            allowed_clusters=[cluster_uids.get(c, str(c)) for c in policy.allowed_cluster_ids],
            denied_clusters=[cluster_uids.get(c, str(c)) for c in policy.denied_cluster_ids],
            rules=policy.rules,
            enabled=policy.enabled,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
            revision=_revision(policy),
            attachments=attachments,
        )

    async def _resolve_attachments(self, rows: list[PolicyAttachment]) -> list[PolicyAttachmentSchema]:
        """Turns polymorphic keys into public references, one query per type.

        `subject_id` has no foreign key; a trigger per subject table deletes an
        attachment with its subject. A row that still resolves to nothing
        serialises with a null reference instead of being dropped.
        """
        by_type: dict[str, list] = {}
        for row in rows:
            by_type.setdefault(row.subject_type, []).append(row.subject_id)
        resolved: dict[str, dict] = {}
        for subject_type, ids in by_type.items():
            resolved[subject_type] = await self.repo.subjects(subject_type, ids)
        out = []
        for row in rows:
            found = resolved.get(row.subject_type, {}).get(row.subject_id)
            out.append(
                PolicyAttachmentSchema(
                    subject_type=row.subject_type,
                    subject_id=str(row.subject_id),
                    subject=found[0] if found else None,
                    subject_name=found[1] if found else None,
                )
            )
        return out

    async def _hydrate(self, policies: list[Policy]) -> list[PolicySchema]:
        grouped = await self.repo.attachments([p.id for p in policies])
        cluster_ids = {c for p in policies for c in list(p.allowed_cluster_ids) + list(p.denied_cluster_ids)}
        cluster_uids = await self.repo.cluster_uids(sorted(cluster_ids, key=str))
        items = []
        for policy in policies:
            attachments = await self._resolve_attachments(grouped.get(policy.id, []))
            items.append(self.schema(policy, attachments, cluster_uids))
        return items

    async def _one(self, ref: str, lock: bool = False) -> Policy:
        policy = await self.repo.get(ref, lock=lock)
        if policy is None:
            raise NotFound(f"no policy {ref!r}")
        return policy

    async def get(self, ref: str) -> PolicySchema:
        return (await self._hydrate([await self._one(ref)]))[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        enabled: bool | None = None,
        region: str | None = None,
        device_class: str | None = None,
        search: str | None = None,
        subject_type: str | None = None,
        subject: str | None = None,
    ) -> ListResponse[PolicySchema]:
        size = clamp_limit(limit)
        subject_ids = None
        if subject is not None:
            if subject_type is None:
                raise Invalid("subject requires subject_type", {"allowed": sorted(SUBJECT_TYPES)})
            check_subject_type(subject_type)
            found = await self.repo.subject(subject_type, subject)
            if found is None:
                raise NotFound(f"no {subject_type} {subject!r}")
            subject_ids = self.repo.policy_ids_for_subject(subject_type, found.id)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            enabled=enabled,
            region=region,
            device_class=device_class,
            search=search,
            subject_ids=subject_ids,
        )
        kept, next_cursor = page(rows, size, lambda p: p.seq)
        items = await self._hydrate(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def _cluster_keys(self, refs: list[str]) -> list:
        keys = []
        for ref in refs:
            cluster = await self.repo.cluster_by_ref(ref)
            if cluster is None:
                raise NotFound(f"no cluster {ref!r}")
            keys.append(cluster.id)
        return keys

    async def _attach(self, policy: Policy, requests: list[PolicyAttachmentRequest]) -> None:
        for request in requests:
            check_subject_type(request.subject_type)
            found = await self.repo.subject(request.subject_type, request.subject)
            if found is None:
                raise NotFound(f"no {request.subject_type} {request.subject!r}")
            await self.repo.add_attachment(
                PolicyAttachment(
                    policy_id=policy.id,
                    subject_type=request.subject_type,
                    subject_id=found.id,
                )
            )

    async def create(self, data: PolicyCreateRequest) -> PolicySchema:
        if not data.name:
            raise Invalid("name is required")
        allowed_keys = await self._cluster_keys(data.allowed_clusters)
        denied_keys = await self._cluster_keys(data.denied_clusters)
        check_cluster_lists([str(k) for k in allowed_keys], [str(k) for k in denied_keys])
        policy = Policy(
            uid=new_uid(POLICY),
            name=data.name,
            description=data.description,
            allowed_regions=data.allowed_regions,
            allowed_residencies=data.allowed_residencies,
            allowed_device_classes=data.allowed_device_classes,
            allowed_cluster_ids=allowed_keys,
            denied_cluster_ids=denied_keys,
            rules=data.rules,
            enabled=data.enabled,
        )
        try:
            await self.repo.add(policy)
            await self._attach(policy, data.attachments)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"policy {data.name!r} already exists") from exc
        return await self.get(policy.uid)

    async def update(self, ref: str, data: PolicyUpdateRequest, if_match: str | None = None) -> PolicySchema:
        policy = await self._one(ref, lock=True)
        if if_match is not None and if_match != _revision(policy):
            raise Conflict("policy changed since it was read", {"revision": _revision(policy)})

        fields = data.to_dict(exclude_unset=True)
        attachments = fields.pop("attachments", UNSET)
        allowed = fields.pop("allowed_clusters", UNSET)
        denied = fields.pop("denied_clusters", UNSET)
        for key, value in fields.items():
            setattr(policy, key, value)

        allowed_keys = await self._cluster_keys(allowed) if allowed is not UNSET else list(policy.allowed_cluster_ids)
        denied_keys = await self._cluster_keys(denied) if denied is not UNSET else list(policy.denied_cluster_ids)
        check_cluster_lists([str(k) for k in allowed_keys], [str(k) for k in denied_keys])
        policy.allowed_cluster_ids = allowed_keys
        policy.denied_cluster_ids = denied_keys

        try:
            if attachments is not UNSET:
                await self.repo.drop_attachments(policy.id)
                await self._attach(policy, attachments)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("policy conflicts with an existing one") from exc
        return await self.get(policy.uid)

    async def delete(self, ref: str) -> None:
        """Attachments go with the policy.

        `PolicyAttachment` cascades on delete, and that is right: an attachment
        is not an object anyone holds independently, it is the binding itself,
        and a binding to a policy that no longer exists constrains nothing.
        """
        policy = await self._one(ref)
        await self.repo.delete(policy)
        await self.db.commit()
