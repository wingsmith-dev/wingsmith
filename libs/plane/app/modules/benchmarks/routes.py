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
from .schemas import BenchmarkBandSchema
from .schemas import BenchmarkHarnessSchema
from .schemas import BenchmarkRequestSchema
from .schemas import BenchmarkVerdictSchema
from .schemas import BenchmarkRunCreateRequest
from .schemas import BenchmarkRequestCreateRequest
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
        request: str | None = None,
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
            request=request,
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

    @post("/requests")
    async def create_request(
        self, context: RequestContext, svc: BenchmarkService, data: BenchmarkRequestCreateRequest
    ) -> Response[BenchmarkRequestSchema]:
        """What to measure and at which level, expanded into runs here.

        A separate collection from `/benchmarks` because a request and a run are
        different objects: one is the ask, the other is a single execution of it,
        and a sweep has one of the first and six of the second.
        """
        return Response(await svc.create_request(data), status_code=201)

    @get("/requests/{ref:str}")
    async def get_request(
        self, context: RequestContext, svc: BenchmarkService, ref: str
    ) -> Response[BenchmarkRequestSchema]:
        return Response(await svc.get_request(ref))

    @get("/requests/{ref:str}/verdict")
    async def get_request_verdict(
        self, context: RequestContext, svc: BenchmarkService, ref: str, criteria: str | None = None
    ) -> Response[BenchmarkVerdictSchema]:
        return Response(await svc.request_verdict(ref, criteria))

    @get("/requests/{ref:str}/bands")
    async def get_request_bands(
        self, context: RequestContext, svc: BenchmarkService, ref: str
    ) -> Response[ListResponse[BenchmarkBandSchema]]:
        """One request's runs, re-aggregated by the input-length band each landed in.

        The standard sets a time-to-first-token target per band, and one
        aggregate number over a whole sweep evidences none of them.
        """
        return Response(await svc.bands(ref))

    @get("/{ref:str}")
    async def get_run(self, context: RequestContext, svc: BenchmarkService, ref: str) -> Response[BenchmarkRunSchema]:
        return Response(await svc.get(ref))

    @get("/{ref:str}/verdict")
    async def get_verdict(
        self, context: RequestContext, svc: BenchmarkService, ref: str, criteria: str | None = None
    ) -> Response[BenchmarkVerdictSchema]:
        """Whether the run met the standard, derived now and stored nowhere.

        `?criteria=` grades against a version other than the one the request
        recorded, which answers "would this have passed under the new rules" —
        a question a stored verdict cannot answer at all.
        """
        return Response(await svc.verdict(ref, criteria))
