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
from .resolve import ResolvedArtifact
from .schemas import ArtifactSchema
from .schemas import ArtifactUsageSchema
from .schemas import ArtifactCreateRequest
from .schemas import ArtifactUpdateRequest
from .services import ArtifactService


class ArtifactRoutes(Controller):
    tags = ["Artifacts"]
    path = "/artifacts"
    dependencies = {"svc": Provide(artifact_svc_generator)}

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
        """Recipe, workload and downstream counts for this artifact. Derived, stored nowhere."""
        return Response(await svc.usage(ref))

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
