"""Policies: the placement constraints, and what they bind to.

A policy is addressed by name or by id. Its subjects are not a nested
sub-resource — `libs/packages/contract/API.md` §6 addresses those by filter, so
`GET /v1/policies?subject_type=project&subject=research` answers "what constrains
this project" and `attachments` on the detail response answers the reverse.

Ids here are bare UUIDs: `Policy` has no `uid` column and §3's prefix list has
nothing left for it.
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

from .depends import policy_svc_generator
from .schemas import PolicySchema
from .schemas import PolicyCreateRequest
from .schemas import PolicyUpdateRequest
from .services import PolicyService


class PolicyRoutes(Controller):
    tags = ["Policies"]
    path = "/policies"
    dependencies = {"svc": Provide(policy_svc_generator)}

    @get()
    async def list_policies(
        self,
        context: RequestContext,
        svc: PolicyService,
        limit: int | None = None,
        cursor: str | None = None,
        enabled: bool | None = None,
        region: str | None = None,
        device_class: str | None = Parameter(query="class", default=None, required=False),
        search: str | None = None,
        subject_type: str | None = None,
        subject: str | None = None,
    ) -> Response[ListResponse[PolicySchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            enabled=enabled,
            region=region,
            device_class=device_class,
            search=search,
            subject_type=subject_type,
            subject=subject,
        )
        return Response(result)

    @post()
    async def create_policy(
        self, context: RequestContext, svc: PolicyService, data: PolicyCreateRequest
    ) -> Response[PolicySchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/{ref:str}")
    async def get_policy(self, context: RequestContext, svc: PolicyService, ref: str) -> Response[PolicySchema]:
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_policy(
        self,
        context: RequestContext,
        svc: PolicyService,
        ref: str,
        data: PolicyUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[PolicySchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_policy(self, context: RequestContext, svc: PolicyService, ref: str) -> None:
        await svc.delete(ref)
