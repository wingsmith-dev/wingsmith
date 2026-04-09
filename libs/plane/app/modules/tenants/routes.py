"""Tenants: the billing and isolation root a project hangs off.

The collection exists because a project cannot be created without one and nothing
else could make one. It is deliberately thin — a slug, a name and a switch — and
it is not a place to put access control, billing accounts or contacts; those are
their own objects when they exist, not fields smuggled onto this one.
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

from .depends import tenant_svc_generator
from .schemas import TenantSchema
from .schemas import TenantCreateRequest
from .schemas import TenantUpdateRequest
from .services import TenantService


class TenantRoutes(Controller):
    tags = ["Tenants"]
    path = "/tenants"
    dependencies = {"svc": Provide(tenant_svc_generator)}

    @get()
    async def list_tenants(
        self,
        context: RequestContext,
        svc: TenantService,
        limit: int | None = None,
        cursor: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Response[ListResponse[TenantSchema]]:
        result = await svc.list(limit=limit, cursor=cursor, enabled=enabled, search=search)
        return Response(result)

    @post()
    async def create_tenant(
        self, context: RequestContext, svc: TenantService, data: TenantCreateRequest
    ) -> Response[TenantSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_tenant(self, context: RequestContext, svc: TenantService, ref: str) -> Response[TenantSchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_tenant(
        self,
        context: RequestContext,
        svc: TenantService,
        ref: str,
        data: TenantUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[TenantSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_tenant(self, context: RequestContext, svc: TenantService, ref: str) -> None:
        await svc.delete(ref)
