from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar import patch
from litestar import delete
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext
from app.core.filters import parse_labels
from app.core.schemas import ListResponse

from .depends import artifact_svc_generator
from .depends import artifact_baseline_svc_generator
from .resolve import ResolvedArtifact
from .schemas import FLEET
from .schemas import ArtifactSchema
from .schemas import ArtifactUsageSchema
from .schemas import ArtifactCreateRequest
from .schemas import ArtifactFitmentSchema
from .schemas import ArtifactRefreshSchema
from .schemas import ArtifactUpdateRequest
from .schemas import ArtifactBaselineSchema
from .schemas import ArtifactFitmentRequest
from .schemas import ArtifactBaselineCreateRequest
from .schemas import ArtifactBaselineUpdateRequest
from .services import ArtifactService
from .services import ArtifactBaselineService


class ArtifactRoutes(Controller):
    tags = ["Artifacts"]
    path = "/artifacts"
    dependencies = {
        "svc": Provide(artifact_svc_generator),
        "baselines": Provide(artifact_baseline_svc_generator),
    }

    @get()
    async def list_artifacts(
        self,
        context: RequestContext,
        svc: ArtifactService,
        limit: int | None = None,
        cursor: str | None = None,
        kind: str | None = None,
        artifact_format: str | None = Parameter(query="format", default=None),
        project: str | None = None,
        base_artifact: str | None = None,
        search: str | None = None,
        label: list[str] | None = None,
    ) -> Response[ListResponse[ArtifactSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            kind=kind,
            format=artifact_format,
            project=project,
            base_artifact=base_artifact,
            search=search,
            labels=parse_labels(label),
        )
        return Response(result)

    @post()
    async def create_artifact(
        self, context: RequestContext, svc: ArtifactService, data: ArtifactCreateRequest
    ) -> Response[ArtifactSchema]:
        return Response(await svc.create(data), status_code=201)

    @get("/resolve")
    async def resolve_artifact(
        self, context: RequestContext, svc: ArtifactService, source: str
    ) -> Response[ResolvedArtifact]:
        """Registered ahead of `/{ref}` so the literal path cannot be read as a ref."""
        return Response(await svc.resolve(source))

    @get("/{ref:str}")
    async def get_artifact(self, context: RequestContext, svc: ArtifactService, ref: str) -> Response[ArtifactSchema]:
        return Response(await svc.get(ref))

    @get("/{ref:str}/usage")
    async def artifact_usage(
        self, context: RequestContext, svc: ArtifactService, ref: str
    ) -> Response[ArtifactUsageSchema]:
        """Workload and downstream counts for this artifact. Derived, stored nowhere."""
        return Response(await svc.usage(ref))

    @get("/{ref:str}/fitment")
    async def artifact_fitment(
        self,
        context: RequestContext,
        svc: ArtifactService,
        ref: str,
        fit_scope: str = Parameter(query="scope", default=FLEET),
        context_length: int | None = None,
        concurrency: int = 1,
        quantization: str | None = None,
        devices_per_node: int | None = None,
        max_devices: int | None = None,
        limit: int | None = None,
    ) -> Response[ArtifactFitmentSchema]:
        """The device classes this artifact could be served on, ranked, derived at read time.

        A `GET` with query parameters rather than a posted body: nothing is created,
        the same question always returns the same answer, and a link to one matrix is
        a thing a person can send to another person.

        The `scope` query parameter binds to `fit_scope` because `scope` is the name
        Litestar reserves for the ASGI scope, and a handler argument called that
        receives the connection instead of the query string — silently, and as a value
        that fails validation rather than one that fails to parse.
        """
        return Response(
            await svc.fitment(
                ref,
                ArtifactFitmentRequest(
                    scope=fit_scope,
                    context_length=context_length,
                    concurrency=concurrency,
                    quantization=quantization,
                    devices_per_node=devices_per_node,
                    max_devices=max_devices,
                    limit=limit,
                ),
            )
        )

    @post("/{ref:str}/refresh")
    async def refresh_artifact(
        self, context: RequestContext, svc: ArtifactService, ref: str
    ) -> Response[ArtifactRefreshSchema]:
        """Re-reads this artifact's source and records what the plane can now derive from it.

        No body: the source is already on the artifact, and letting a caller name a
        different one would be a way to move an artifact onto weights it was not
        registered from. A source that resolves to a different digest is a 409.
        """
        return Response(await svc.refresh(ref))

    @get("/{ref:str}/baselines")
    async def list_artifact_baselines(
        self,
        context: RequestContext,
        baselines: ArtifactBaselineService,
        ref: str,
        limit: int | None = None,
        cursor: str | None = None,
        dataset: str | None = None,
        metric: str | None = None,
    ) -> Response[ListResponse[ArtifactBaselineSchema]]:
        """The declared quality floors for this artifact, one row per dataset and metric."""
        return Response(await baselines.list(ref, limit=limit, cursor=cursor, dataset=dataset, metric=metric))

    @post("/{ref:str}/baselines")
    async def create_artifact_baseline(
        self,
        context: RequestContext,
        baselines: ArtifactBaselineService,
        ref: str,
        data: ArtifactBaselineCreateRequest,
    ) -> Response[ArtifactBaselineSchema]:
        return Response(await baselines.create(ref, data), status_code=201)

    @patch("/{ref:str}/baselines/{baseline:str}")
    async def update_artifact_baseline(
        self,
        context: RequestContext,
        baselines: ArtifactBaselineService,
        ref: str,
        baseline: str,
        data: ArtifactBaselineUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ArtifactBaselineSchema]:
        return Response(await baselines.update(ref, baseline, data, if_match=if_match))

    @delete("/{ref:str}/baselines/{baseline:str}", status_code=204)
    async def delete_artifact_baseline(
        self, context: RequestContext, baselines: ArtifactBaselineService, ref: str, baseline: str
    ) -> None:
        await baselines.delete(ref, baseline)

    @patch("/{ref:str}")
    async def update_artifact(
        self,
        context: RequestContext,
        svc: ArtifactService,
        ref: str,
        data: ArtifactUpdateRequest,
        if_match: str | None = Parameter(header="If-Match", default=None),
    ) -> Response[ArtifactSchema]:
        return Response(await svc.update(ref, data, if_match=if_match))

    @delete("/{ref:str}", status_code=204)
    async def delete_artifact(self, context: RequestContext, svc: ArtifactService, ref: str) -> None:
        await svc.delete(ref)
