import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import operation_svc_generator
from .schemas import OperationSchema
from .schemas import OperationCreateRequest
from .services import OperationService


class OperationRoutes(Controller):
    """One collection for every imperative action in the system.

    There is no `POST /v1/nodes/{id}/drain` anywhere, and that is the whole
    point: a drain is long-running, has an actor and a reason, and can be stuck.
    A verb endpoint returning 200 leaves "who started this and why is it stuck"
    with nowhere to be answered from.
    """

    tags = ["Operations"]
    path = "/operations"
    dependencies = {"svc": Provide(operation_svc_generator)}

    @get()
    async def list_operations(
        self,
        context: RequestContext,
        svc: OperationService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        kind: str | None = Parameter(query="type", default=None),
        phase: str | None = None,
        target: str | None = None,
        actor: str | None = None,
        since: dt.datetime | None = None,
    ) -> Response[ListResponse[OperationSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            kind=kind,
            phase=phase,
            target=target,
            actor=actor,
            since=since,
        )
        return Response(result)

    @post()
    async def create_operation(
        self,
        context: RequestContext,
        svc: OperationService,
        data: OperationCreateRequest,
        idempotency_key: str | None = Parameter(header="Idempotency-Key", default=None),
    ) -> Response[OperationSchema]:
        return Response(await svc.create(data, idempotency_key=idempotency_key), status_code=201)

    @get("/{ref:str}")
    async def get_operation(
        self, context: RequestContext, svc: OperationService, ref: str
    ) -> Response[OperationSchema]:
        return Response(await svc.get(ref))

    @delete("/{ref:str}", status_code=200)
    async def cancel_operation(
        self, context: RequestContext, svc: OperationService, ref: str
    ) -> Response[OperationSchema]:
        """Cancellation, and deliberately not deletion.

        Deleting the row would throw away the answer to who asked for this and
        why, which is the reason the object exists. A pending operation moves to
        `cancelled` outright; a running one has `cancel_requested_at` stamped and
        reaches its terminal phase when the cluster reports it.
        """
        return Response(await svc.cancel(ref))
