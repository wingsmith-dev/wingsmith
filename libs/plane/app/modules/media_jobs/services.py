"""The async media job queue (ADR 0041).

The plane holds the rows and nothing else: a gateway worker claims a job under a lease, runs it
against the cluster, writes the output to object storage and reports the key back. A lease that
lapses puts the job back in the queue until it has used `MEDIA_JOB_MAX_ATTEMPTS` claims.
"""

import logging
import datetime as dt

from typing import Protocol

from sqlalchemy import or_
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.ids import MEDIA_JOB
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import Project
from app.db.models import Endpoint
from app.db.models import MediaJob
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound

from .schemas import MediaJobSchema
from .schemas import MediaJobClaimRequest
from .schemas import MediaJobCreateRequest
from .schemas import MediaJobCompleteRequest
from .schemas import MediaJobHeartbeatSchema
from .schemas import MediaJobHeartbeatRequest


logger = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")
MAX_REQUEST_KEYS = 256


class ObjectStore(Protocol):
    async def delete(self, key: str) -> None: ...


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _schema(
    job: MediaJob, tenant: str, project: str, endpoint: str | None, with_request: bool = False
) -> MediaJobSchema:
    return MediaJobSchema(
        id=job.uid,
        tenant=tenant,
        project=project,
        endpoint=endpoint,
        kind=job.kind,
        path=job.path,
        model=job.model,
        status=job.status,  # type: ignore[arg-type]
        progress=job.progress,
        attempts=job.attempts,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        expires_at=job.retention_until,
        object_key=job.object_key,
        content_type=job.content_type,
        size_bytes=job.size_bytes,
        upstream_seconds=float(job.upstream_seconds) if job.upstream_seconds is not None else None,
        units=job.units,
        error=job.error,
        request=job.request if with_request else None,
    )


class MediaJobService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _joined(self):
        return (
            select(MediaJob, Tenant.uid, Project.slug, Endpoint.uid)
            .join(Tenant, Tenant.id == MediaJob.tenant_id)
            .join(Project, Project.id == MediaJob.project_id)
            .outerjoin(Endpoint, Endpoint.id == MediaJob.endpoint_id)
        )

    async def _one(self, *where, with_request: bool = False) -> MediaJobSchema | None:
        row = (await self.db.execute(self._joined().where(*where))).first()
        if row is None:
            return None
        return _schema(row[0], row[1], row[2], row[3], with_request=with_request)

    async def _require(self, uid: str, tenant: str | None) -> MediaJobSchema:
        where = [MediaJob.uid == uid]
        if tenant is not None:
            where.append(Tenant.uid == tenant)
        job = await self._one(*where)
        if job is None:
            raise NotFound(f"media job {uid} not found")
        return job

    async def create(self, data: MediaJobCreateRequest) -> tuple[MediaJobSchema, bool]:
        """Queues a job, or returns the one this tenant already queued under the same key."""
        if len(data.request) > MAX_REQUEST_KEYS:
            raise Invalid("request has too many fields")
        row = (
            await self.db.execute(
                select(Tenant.id, Endpoint.id, Endpoint.project_id)
                .join(Project, Project.tenant_id == Tenant.id)
                .join(Endpoint, Endpoint.project_id == Project.id)
                .where(Tenant.uid == data.tenant, Endpoint.uid == data.endpoint)
            )
        ).first()
        if row is None:
            # One answer for an unknown tenant, an unknown endpoint and another tenant's endpoint.
            raise NotFound(f"endpoint {data.endpoint} not found for tenant {data.tenant}")
        tenant_id, endpoint_id, project_id = row
        if data.idempotency_key:
            existing = await self._one(
                MediaJob.tenant_id == tenant_id, MediaJob.idempotency_key == data.idempotency_key
            )
            if existing is not None:
                return existing, False
        job = MediaJob(
            uid=new_uid(MEDIA_JOB),
            tenant_id=tenant_id,
            project_id=project_id,
            endpoint_id=endpoint_id,
            kind=data.kind,
            path=data.path,
            model=data.model,
            request=data.request,
            idempotency_key=data.idempotency_key or None,
        )
        self.db.add(job)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing = await self._one(
                MediaJob.tenant_id == tenant_id, MediaJob.idempotency_key == data.idempotency_key
            )
            if existing is None:
                raise
            return existing, False
        return await self._require(job.uid, None), True

    async def get(self, uid: str, tenant: str | None) -> MediaJobSchema:
        return await self._require(uid, tenant)

    async def cancel(self, uid: str, tenant: str | None, now: dt.datetime | None = None) -> MediaJobSchema:
        """Cancels a queued job, asks a running one to stop, and expires a finished one now so the
        reactor deletes its output (what OpenAI's `DELETE /v1/videos/{id}` does)."""
        now = now or _now()
        job = await self._require(uid, tenant)
        values: dict
        if job.status == "queued":
            values = {"status": "cancelled", "completed_at": now, "retention_until": now}
        elif job.status == "running":
            values = {"cancel_requested": True}
        else:
            values = {"retention_until": now}
        await self.db.execute(update(MediaJob).where(MediaJob.uid == uid).values(**values))
        await self.db.commit()
        return await self._require(uid, None)

    async def expire_leases(self, now: dt.datetime) -> None:
        """Fails, or cancels, a running job whose lease lapsed and which may not be claimed again."""
        lapsed = and_(MediaJob.status == "running", MediaJob.lease_expires_at < now)
        await self.db.execute(
            update(MediaJob)
            .where(lapsed, MediaJob.cancel_requested)
            .values(status="cancelled", completed_at=now, retention_until=now, lease_owner=None, lease_expires_at=None)
        )
        await self.db.execute(
            update(MediaJob)
            .where(lapsed, MediaJob.attempts >= settings.MEDIA_JOB_MAX_ATTEMPTS)
            .values(
                status="failed",
                completed_at=now,
                retention_until=now,
                lease_owner=None,
                lease_expires_at=None,
                error={"code": "lease_expired", "message": "the job's worker stopped reporting too many times"},
            )
        )

    async def claim(self, data: MediaJobClaimRequest, now: dt.datetime | None = None) -> MediaJobSchema | None:
        """Leases the oldest runnable job to a worker: a queued one, or a running one whose lease lapsed."""
        now = now or _now()
        await self.expire_leases(now)
        runnable = or_(
            MediaJob.status == "queued",
            and_(MediaJob.status == "running", MediaJob.lease_expires_at < now),
        )
        pick = select(MediaJob.id).where(runnable).order_by(MediaJob.seq).limit(1).with_for_update(skip_locked=True)
        if data.kinds:
            pick = pick.where(MediaJob.kind.in_(data.kinds))
        claimed = (
            await self.db.execute(
                update(MediaJob)
                .where(MediaJob.id == pick.scalar_subquery())
                .values(
                    status="running",
                    attempts=MediaJob.attempts + 1,
                    lease_owner=data.worker,
                    lease_expires_at=now + dt.timedelta(seconds=settings.MEDIA_JOB_LEASE_SECONDS),
                    started_at=func.coalesce(MediaJob.started_at, now),
                )
                .returning(MediaJob.id)
            )
        ).scalar_one_or_none()
        await self.db.commit()
        if claimed is None:
            return None
        return await self._one(MediaJob.id == claimed, with_request=True)

    @staticmethod
    def _leased(uid: str, worker: str):
        return and_(MediaJob.uid == uid, MediaJob.status == "running", MediaJob.lease_owner == worker)

    async def heartbeat(
        self, uid: str, data: MediaJobHeartbeatRequest, now: dt.datetime | None = None
    ) -> MediaJobHeartbeatSchema:
        now = now or _now()
        expires = now + dt.timedelta(seconds=settings.MEDIA_JOB_LEASE_SECONDS)
        values: dict = {"lease_expires_at": expires}
        if data.progress is not None:
            values["progress"] = max(0, min(100, data.progress))
        cancel = (
            await self.db.execute(
                update(MediaJob)
                .where(self._leased(uid, data.worker))
                .values(**values)
                .returning(MediaJob.cancel_requested)
            )
        ).scalar_one_or_none()
        await self.db.commit()
        if cancel is None:
            raise Conflict(f"media job {uid} is not leased to {data.worker}")
        return MediaJobHeartbeatSchema(cancel_requested=cancel, lease_expires_at=expires)

    async def complete(self, uid: str, data: MediaJobCompleteRequest, now: dt.datetime | None = None) -> MediaJobSchema:
        now = now or _now()
        if data.status == "succeeded" and not data.object_key:
            raise Invalid("a succeeded job needs its object_key")
        retention = select(Project.media_retention_days).where(Project.id == MediaJob.project_id).scalar_subquery()
        done = (
            await self.db.execute(
                update(MediaJob)
                .where(self._leased(uid, data.worker))
                .values(
                    status=data.status,
                    progress=100 if data.status == "succeeded" else MediaJob.progress,
                    object_key=data.object_key,
                    content_type=data.content_type,
                    size_bytes=data.size_bytes,
                    upstream_seconds=data.upstream_seconds,
                    units=data.units,
                    error=data.error,
                    completed_at=now,
                    retention_until=now + func.make_interval(0, 0, 0, retention),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(MediaJob.id)
            )
        ).scalar_one_or_none()
        await self.db.commit()
        if done is None:
            raise Conflict(f"media job {uid} is not leased to {data.worker}")
        return await self._require(uid, None)

    async def collect_expired(self, store: ObjectStore | None, now: dt.datetime | None = None, limit: int = 500) -> int:
        """Deletes the output, then the row, of every job past its retention. A failed object delete
        keeps the row for the next pass, so an output is never orphaned silently."""
        now = now or _now()
        rows = (
            await self.db.execute(
                select(MediaJob.id, MediaJob.object_key)
                .where(MediaJob.retention_until < now, MediaJob.status.in_(TERMINAL))
                .order_by(MediaJob.retention_until)
                .limit(limit)
            )
        ).all()
        gone = []
        for job_id, key in rows:
            if key and store is not None:
                try:
                    await store.delete(key)
                except Exception:
                    logger.warning("media_job.gc.delete_failed key=%s", key, exc_info=True)
                    continue
            gone.append(job_id)
        if gone:
            await self.db.execute(delete(MediaJob).where(MediaJob.id.in_(gone)))
        await self.db.commit()
        return len(gone)
