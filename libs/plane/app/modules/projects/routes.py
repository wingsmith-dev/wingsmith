"""Projects: the unit quota and priority hang off, beneath a tenant.

Not a request scope: nothing reads a project header, and no request is
filtered by project.

A project carries its tenant as an embedded field rather than a link, so no reader
has to join for it; `/v1/tenants` is where one is made. A path segment takes the
project id or its slug; the slug is unique only within a tenant, so `tenant`
narrows an ambiguous one.

There is no `label` filter: projects carry no labels, and a selector that is
accepted and ignored hands back the unfiltered set to a caller who asked for a
subset.
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

from .depends import project_svc_generator
from .schemas import ProjectSchema
from .schemas import ProjectCreateRequest
from .schemas import ProjectUpdateRequest
from .services import ProjectService


class ProjectRoutes(Controller):
    tags = ["Projects"]
    path = "/projects"
    dependencies = {"svc": Provide(project_svc_generator)}

    @get()
    async def list_projects(
        self,
        context: RequestContext,
        svc: ProjectService,
        limit: int | None = None,
        cursor: str | None = None,
        tenant: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Response[ListResponse[ProjectSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            tenant=tenant,
            enabled=enabled,
            search=search,
        )
        return Response(result)

    @post()
    async def create_project(
        self, context: RequestContext, svc: ProjectService, data: ProjectCreateRequest
    ) -> Response[ProjectSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_project(
        self, context: RequestContext, svc: ProjectService, ref: str, tenant: str | None = None
    ) -> Response[ProjectSchema]:
        return Response(await svc.get(ref, tenant=tenant))

    @patch("/{ref:str}")
    async def update_project(
        self,
        context: RequestContext,
        svc: ProjectService,
        ref: str,
        data: ProjectUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ProjectSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_project(self, context: RequestContext, svc: ProjectService, ref: str) -> None:
        await svc.delete(ref)
