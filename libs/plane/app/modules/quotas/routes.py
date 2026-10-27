"""Quotas: a project's device floor and ceiling, per device class.

Two unrelated objects live here and the names are close enough to be dangerous.
A `Quota` is devices, checked once by placement when a workload is admitted. A
`ServingQuota` is traffic (RPM, TPM, concurrency) declared for an endpoint and
not enforced yet: the gateway does not read serving quotas, so every limit here
is advisory until it does. Different table, different consumer, and no field in
common.

Declared limits and observed usage arrive together and stay apart: `usage` is
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
from .depends import serving_quota_svc_generator
from .schemas import QuotaSchema
from .schemas import QuotaCreateRequest
from .schemas import QuotaUpdateRequest
from .schemas import ServingQuotaSchema
from .schemas import ServingQuotaCreateRequest
from .schemas import ServingQuotaUpdateRequest
from .schemas import ResolvedServingQuotaTableSchema
from .services import QuotaService
from .services import ServingQuotaService


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


class ServingQuotaRoutes(Controller):
    """Serving quotas: the traffic a served endpoint is committed to carry.

    Declared here and not enforced anywhere yet: the gateway does not read these,
    so `enforced` records what the operator wants once it does, and every limit is
    advisory today. No handler reads a counter, calls the metrics backend or reports a rate: the
    observed half of these numbers is on the metrics surface, and folding it in
    here would put the data plane's measurements on the control plane's read path.
    """

    tags = ["Serving Quotas"]
    path = "/serving-quotas"
    dependencies = {"svc": Provide(serving_quota_svc_generator)}

    @get()
    async def list_serving_quotas(
        self,
        context: RequestContext,
        svc: ServingQuotaService,
        limit: int | None = None,
        cursor: str | None = None,
        endpoint: str | None = None,
        project: str | None = None,
        enforced: bool | None = None,
        limit_scope: str | None = Parameter(query="scope", default=None, required=False),
    ) -> Response[ListResponse[ServingQuotaSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            endpoint=endpoint,
            project=project,
            enforced=enforced,
            limit_scope=limit_scope,
        )
        return Response(result)

    @post()
    async def create_serving_quota(
        self, context: RequestContext, svc: ServingQuotaService, data: ServingQuotaCreateRequest
    ) -> Response[ServingQuotaSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/resolved")
    async def resolve_serving_quotas(
        self,
        context: RequestContext,
        svc: ServingQuotaService,
        endpoint: str | None = None,
        workload: str | None = None,
        replicas: int = 1,
        replica_index: int = 0,
        if_none_match: str | None = Parameter(header="If-None-Match", default=None),
    ) -> Response[ResolvedServingQuotaTableSchema | None]:
        """What each gateway replica would enforce, for a fleet of a stated size.

        No gateway reads this table yet; it is the shape enforcement will consume.

        Unpaginated, like the routing table: a caller that assembled half of it
        would enforce half its commitments. `replicas` and `replica_index` are the
        fleet the shares are taken against: the plane cannot discover them
        without putting a Kubernetes client on the inference path, so the caller
        states them and the arithmetic happens once, here, not once per
        client. They default to a fleet of one, where every mode agrees.

        `endpoint` and `workload` narrow the table. A workload is a lookup and
        never a key: it comes back with one entry per endpoint its traffic is
        admitted through, never a sum, because nobody sold the sum.

        `If-None-Match` makes the steady state a `304`, which is what polling this
        on a timer should cost. The revision covers the fleet as well as the rows,
        so a caller that comes back with a different `replicas` is answered rather
        than told nothing changed.
        """
        table = await svc.resolved(
            endpoint=endpoint,
            workload=workload,
            replicas=replicas,
            replica_index=replica_index,
        )
        if if_none_match is not None and if_none_match == table.revision:
            return Response(None, status_code=304, headers={"ETag": table.revision})
        return Response(table, headers={"ETag": table.revision})

    @get("/{ref:str}")
    async def get_serving_quota(
        self, context: RequestContext, svc: ServingQuotaService, ref: str
    ) -> Response[ServingQuotaSchema]:
        """Declared fields only. An inventory read that touches one table."""
        return Response(await svc.get(ref))

    @patch("/{ref:str}")
    async def update_serving_quota(
        self,
        context: RequestContext,
        svc: ServingQuotaService,
        ref: str,
        data: ServingQuotaUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ServingQuotaSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_serving_quota(self, context: RequestContext, svc: ServingQuotaService, ref: str) -> None:
        await svc.delete(ref)
