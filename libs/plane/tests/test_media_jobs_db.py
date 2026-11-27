"""The async media job queue against a real Postgres (ADR 0041)."""

import datetime as dt

import pytest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.modules.projects.schemas import ProjectCreateRequest
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.projects.services import ProjectService
from app.modules.endpoints.services import EndpointService
from app.modules.media_jobs.schemas import MediaJobClaimRequest
from app.modules.media_jobs.schemas import MediaJobCreateRequest
from app.modules.media_jobs.schemas import MediaJobCompleteRequest
from app.modules.media_jobs.schemas import MediaJobHeartbeatRequest
from app.modules.media_jobs.services import MediaJobService

from tests.test_modules_db import seed_tenant


pytestmark = pytest.mark.database

T0 = dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.UTC)


async def an_endpoint(db: AsyncSession, tenant: str, project: str, slug: str) -> tuple[str, str]:
    tenant_uid = await seed_tenant(db, tenant)
    await ProjectService(db).create(ProjectCreateRequest(tenant=tenant, slug=project, name=project.title()))
    endpoint = await EndpointService(db).create(EndpointCreateRequest(name="video", project=project, slug=slug))
    return tenant_uid, endpoint.id


def a_job(tenant: str, endpoint: str, key: str | None = None) -> MediaJobCreateRequest:
    return MediaJobCreateRequest(
        tenant=tenant,
        endpoint=endpoint,
        kind="video",
        path="/v1/videos",
        model="video",
        request={"model": "video", "prompt": "a cat", "seconds": "4"},
        idempotency_key=key,
    )


async def test_a_job_runs_from_queued_to_succeeded_and_gets_its_projects_retention(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    await db.execute(text("UPDATE bind_project SET media_retention_days = 3"))
    await db.commit()
    svc = MediaJobService(db)
    job, created = await svc.create(a_job(tenant, endpoint))
    assert created and job.status == "queued" and job.project == "search"

    claimed = await svc.claim(MediaJobClaimRequest(worker="gw-1"), now=T0)
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == "running" and claimed.attempts == 1
    assert claimed.request == {"model": "video", "prompt": "a cat", "seconds": "4"}
    assert await svc.claim(MediaJobClaimRequest(worker="gw-2"), now=T0) is None

    beat = await svc.heartbeat(job.id, MediaJobHeartbeatRequest(worker="gw-1", progress=40), now=T0)
    assert not beat.cancel_requested
    assert (await svc.get(job.id, tenant)).progress == 40

    done = await svc.complete(
        job.id,
        MediaJobCompleteRequest(
            worker="gw-1",
            status="succeeded",
            object_key=f"media/search/{job.id}",
            content_type="video/mp4",
            size_bytes=1234,
            upstream_seconds=42.5,
            units={"video_seconds": 4},
        ),
        now=T0,
    )
    assert done.status == "succeeded" and done.progress == 100
    assert done.upstream_seconds == 42.5
    assert done.expires_at == T0 + dt.timedelta(days=3)


async def test_an_expired_lease_requeues_until_attempts_run_out(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    svc = MediaJobService(db)
    job, _ = await svc.create(a_job(tenant, endpoint))
    lease = dt.timedelta(seconds=settings.MEDIA_JOB_LEASE_SECONDS + 1)

    moment = T0
    for attempt in range(1, settings.MEDIA_JOB_MAX_ATTEMPTS + 1):
        claimed = await svc.claim(MediaJobClaimRequest(worker=f"gw-{attempt}"), now=moment)
        assert claimed is not None and claimed.attempts == attempt
        moment += lease

    # The worker that lost its lease can no longer report.
    with pytest.raises(Conflict):
        await svc.heartbeat(job.id, MediaJobHeartbeatRequest(worker="gw-1"), now=moment)

    assert await svc.claim(MediaJobClaimRequest(worker="gw-x"), now=moment) is None
    failed = await svc.get(job.id, None)
    assert failed.status == "failed" and failed.error["code"] == "lease_expired"


async def test_an_idempotency_key_returns_the_first_job(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    svc = MediaJobService(db)
    first, created = await svc.create(a_job(tenant, endpoint, key="k1"))
    again, created_again = await svc.create(a_job(tenant, endpoint, key="k1"))
    other, _ = await svc.create(a_job(tenant, endpoint, key="k2"))
    assert created and not created_again
    assert again.id == first.id and other.id != first.id


async def test_cancel_stops_a_queued_job_and_flags_a_running_one(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    svc = MediaJobService(db)
    running, _ = await svc.create(a_job(tenant, endpoint))
    await svc.claim(MediaJobClaimRequest(worker="gw-2"), now=T0)
    queued, _ = await svc.create(a_job(tenant, endpoint))

    assert (await svc.cancel(queued.id, tenant, now=T0)).status == "cancelled"
    flagged = await svc.cancel(running.id, tenant, now=T0)
    assert flagged.status == "running" and flagged.cancel_requested
    assert (await svc.heartbeat(running.id, MediaJobHeartbeatRequest(worker="gw-2"), now=T0)).cancel_requested


async def test_a_tenant_sees_and_submits_only_its_own(db: AsyncSession) -> None:
    acme, acme_endpoint = await an_endpoint(db, "acme", "search", "video-a")
    globex, globex_endpoint = await an_endpoint(db, "globex", "ads", "video-b")
    svc = MediaJobService(db)
    job, _ = await svc.create(a_job(acme, acme_endpoint))

    with pytest.raises(NotFound):
        await svc.get(job.id, globex)
    with pytest.raises(NotFound):
        await svc.cancel(job.id, globex)
    with pytest.raises(NotFound):
        await svc.create(a_job(globex, acme_endpoint))
    # One tenant's idempotency key does not collide with another's.
    a, _ = await svc.create(a_job(acme, acme_endpoint, key="same"))
    b, created = await svc.create(a_job(globex, globex_endpoint, key="same"))
    assert created and a.id != b.id and b.project == "ads"


class FakeStore:
    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    async def delete(self, key: str) -> None:
        if key in self.fail:
            raise RuntimeError("store down")
        self.deleted.append(key)


async def finished(svc: MediaJobService, tenant: str, endpoint: str, key: str):
    job, _ = await svc.create(a_job(tenant, endpoint))
    await svc.claim(MediaJobClaimRequest(worker="gw"), now=T0)
    return await svc.complete(job.id, MediaJobCompleteRequest(worker="gw", status="succeeded", object_key=key), now=T0)


async def test_retention_deletes_the_object_then_the_row(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    svc = MediaJobService(db)
    kept = await finished(svc, tenant, endpoint, "media/search/a")
    stuck = await finished(svc, tenant, endpoint, "media/search/b")
    store = FakeStore(fail={"media/search/b"})

    assert await svc.collect_expired(store, now=T0 + dt.timedelta(days=6)) == 0
    assert await svc.collect_expired(store, now=T0 + dt.timedelta(days=8)) == 1
    assert store.deleted == ["media/search/a"]
    with pytest.raises(NotFound):
        await svc.get(kept.id, None)
    # A failed delete keeps the row for the next pass.
    assert (await svc.get(stuck.id, None)).status == "succeeded"


async def test_delete_of_a_finished_job_expires_it_now(db: AsyncSession) -> None:
    tenant, endpoint = await an_endpoint(db, "acme", "search", "video-a")
    svc = MediaJobService(db)
    job = await finished(svc, tenant, endpoint, "media/search/a")
    await svc.cancel(job.id, tenant, now=T0 + dt.timedelta(hours=1))
    store = FakeStore()
    assert await svc.collect_expired(store, now=T0 + dt.timedelta(hours=2)) == 1
    assert store.deleted == ["media/search/a"]
