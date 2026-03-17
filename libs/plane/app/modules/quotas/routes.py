"""Quotas: a project's device floor and ceiling, per device class.

Declared limits and observed usage arrive together and stay apart — `usage` is
counted over live allocations at read time and never folded into the declared
numbers beside it. There is no `label` filter: quotas carry no labels, and a
selector that is silently ignored returns the unfiltered set to a caller who
asked for a subset.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import quota_svc_generator
from .schemas import QuotaSchema
from .schemas import QuotaCreateRequest
from .schemas import QuotaUpdateRequest
from .services import QuotaService


class QuotaRoutes(Controller):
    tags = ["Quotas"]
    path = "/quotas"
    dependencies = {"svc": Provide(quota_svc_generator)}

    @get()
    async def list_quotas(
        self,
        context: RequestContext,
        svc: QuotaService,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        cluster: str | None = None,
        device_class: str | None = Parameter(query="class", default=None, required=False),
        over_quota_enabled: bool | None = None,
    ) -> Response[ListResponse[QuotaSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            project=project,
            cluster=cluster,
            device_class=device_class,
            over_quota_enabled=over_quota_enabled,
        )
        return Response(result)

    @post()
    async def create_quota(
        self, context: RequestContext, svc: QuotaService, data: QuotaCreateRequest
    ) -> Response[QuotaSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_quota(self, context: RequestContext, svc: QuotaService, ref: str) -> Response[QuotaSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_quota(
        self,
        context: RequestContext,
        svc: QuotaService,
        ref: str,
        data: QuotaUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[QuotaSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_quota(self, context: RequestContext, svc: QuotaService, ref: str) -> None:
        await svc.delete(ref)
