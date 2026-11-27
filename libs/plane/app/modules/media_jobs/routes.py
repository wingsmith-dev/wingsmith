"""The async media job queue, for the gateway's service principal only (ADR 0041).

Tenants never call these: they call the gateway with their API key, and the gateway passes the
key's tenant as `tenant` on every read, so a job is only ever visible to the tenant that made it.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar.di import Provide

from app.core.context import RequestContext

from .depends import media_job_svc_generator
from .schemas import MediaJobSchema
from .schemas import MediaJobClaimRequest
from .schemas import MediaJobCreateRequest
from .schemas import MediaJobCompleteRequest
from .schemas import MediaJobHeartbeatSchema
from .schemas import MediaJobHeartbeatRequest
from .services import MediaJobService


class MediaJobRoutes(Controller):
    tags = ["Gateway"]
    path = "/gateway/media-jobs"
    dependencies = {"svc": Provide(media_job_svc_generator)}

    @post()
    async def create_media_job(
        self, context: RequestContext, svc: MediaJobService, data: MediaJobCreateRequest
    ) -> Response[MediaJobSchema]:
        """Queues a job. A replay under the same idempotency key returns the first job with `200`."""
        job, created = await svc.create(data)
        return Response(job, status_code=201 if created else 200)

    @post("/claim")
    async def claim_media_job(
        self, context: RequestContext, svc: MediaJobService, data: MediaJobClaimRequest
    ) -> Response[MediaJobSchema | None]:
        """Leases the oldest runnable job to `worker`, or `204` when there is none."""
        job = await svc.claim(data)
        if job is None:
            return Response(None, status_code=204)
        return Response(job, status_code=200)

    @get("/{uid:str}")
    async def get_media_job(
        self, context: RequestContext, svc: MediaJobService, uid: str, tenant: str | None = None
    ) -> Response[MediaJobSchema]:
        return Response(await svc.get(uid, tenant))

    @post("/{uid:str}/cancel")
    async def cancel_media_job(
        self, context: RequestContext, svc: MediaJobService, uid: str, tenant: str | None = None
    ) -> Response[MediaJobSchema]:
        return Response(await svc.cancel(uid, tenant), status_code=200)

    @post("/{uid:str}/heartbeat")
    async def heartbeat_media_job(
        self, context: RequestContext, svc: MediaJobService, uid: str, data: MediaJobHeartbeatRequest
    ) -> Response[MediaJobHeartbeatSchema]:
        """Extends the lease. `409` when the worker no longer holds it; it must stop."""
        return Response(await svc.heartbeat(uid, data), status_code=200)

    @post("/{uid:str}/complete")
    async def complete_media_job(
        self, context: RequestContext, svc: MediaJobService, uid: str, data: MediaJobCompleteRequest
    ) -> Response[MediaJobSchema]:
        return Response(await svc.complete(uid, data), status_code=200)
