"""A project's credentials: references to secrets held on the clusters, never the secrets.

`PUT` declares one by name, like applying an object, and `DELETE` removes it. A private artifact of
the project is fetched with its `hf_token`; the plane forwards only `secret_ref` to the clusters
that cache the artifact.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import put
from litestar import delete
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .schemas import CredentialSchema
from .schemas import CredentialPutRequest
from .services import CredentialService


async def credential_svc_generator(context: RequestContext):
    yield CredentialService(db=context.db)


class CredentialRoutes(Controller):
    tags = ["Projects"]
    path = "/projects/{project:str}/credentials"
    dependencies = {"svc": Provide(credential_svc_generator)}

    @get()
    async def list_credentials(
        self,
        context: RequestContext,
        svc: CredentialService,
        project: str,
        tenant: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Response[ListResponse[CredentialSchema]]:
        return Response(await svc.list(project, tenant, limit=limit, cursor=cursor))

    @put("/{name:str}")
    async def put_credential(
        self,
        context: RequestContext,
        svc: CredentialService,
        project: str,
        name: str,
        data: CredentialPutRequest,
        tenant: str | None = None,
    ) -> Response[CredentialSchema]:
        schema, created = await svc.put(project, name, data, tenant)
        return Response(schema, status_code=201 if created else 200)

    @delete("/{name:str}", status_code=204)
    async def delete_credential(
        self, context: RequestContext, svc: CredentialService, project: str, name: str, tenant: str | None = None
    ) -> None:
        await svc.delete(project, name, tenant)
