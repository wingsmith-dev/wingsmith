import datetime as dt

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import benchmark_svc_generator
from .schemas import BenchmarkRunSchema
from .schemas import BenchmarkHarnessSchema
from .schemas import BenchmarkRunCreateRequest
from .services import BenchmarkService


class BenchmarkRoutes(Controller):
    """Runs and their results, filtered by the deployment they were taken against.

    A top-level collection rather than `/deployments/{id}/benchmarks`: the run is
    the object, and a deployment is one of several things it is filtered by. The
    console's per-deployment tab is `?deployment=`, which is the same query the
    CLI makes.

    There is no update and no delete. A measurement is not editable, and deleting
    one throws away the history the table exists to be. Stopping a run in flight
    is `DELETE /v1/operations/{id}` against the operation that carries it, which
    is where every other long-running thing is cancelled.
    """

    tags = ["Benchmarks"]
    path = "/benchmarks"
    dependencies = {"svc": Provide(benchmark_svc_generator)}

    @get()
    async def list_runs(
        self,
        context: RequestContext,
        svc: BenchmarkService,
        limit: int | None = None,
        cursor: str | None = None,
        deployment: str | None = None,
        cluster: str | None = None,
        harness: str | None = None,
        phase: str | None = None,
        actor: str | None = None,
        since: dt.datetime | None = None,
    ) -> Response[ListResponse[BenchmarkRunSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            deployment=deployment,
            cluster=cluster,
            harness=harness,
            phase=phase,
            actor=actor,
            since=since,
        )
        return Response(result)

    @post()
    async def create_run(
        self, context: RequestContext, svc: BenchmarkService, data: BenchmarkRunCreateRequest
    ) -> Response[BenchmarkRunSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/harnesses")
    async def list_harnesses(
        self, context: RequestContext, svc: BenchmarkService
    ) -> Response[ListResponse[BenchmarkHarnessSchema]]:
        """What can be run and what it accepts.

        Ahead of `/{ref}` in declaration order because `harnesses` is not an id
        and would otherwise be resolved as one.
        """
        items = svc.harnesses()
        return Response(ListResponse(items=items, count=len(items), next_cursor=None))

    @get("/{ref:str}")
    async def get_run(self, context: RequestContext, svc: BenchmarkService, ref: str) -> Response[BenchmarkRunSchema]:
        return Response(await svc.get(ref))
