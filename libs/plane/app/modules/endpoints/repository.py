from __future__ import annotations

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import ServingQuota
from app.db.models import ClusterServing
from app.db.models import EndpointTarget
from app.core.filters import ref_ids


class EndpointRepository:
    """Reads endpoints and the deployments behind them. No cluster ever appears.

    The only join out of this module goes through `EndpointTarget` to a
    deployment, and it exists to answer "which endpoints serve these weights" —
    not to give the endpoint a location.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Endpoint, Project.uid.label("project_uid"))
            .join(Project, Endpoint.project_id == Project.id)
            .options(selectinload(Endpoint.targets))
        )

    def get_query(self, ref: str) -> Select:
        """An id or a name. An endpoint name is globally unique, so both are exact."""
        column = Endpoint.uid if is_uid(ref) else Endpoint.name
        return self._base().where(column == ref)

    def serving_artifact_query(self, artifact: str) -> Select:
        """The endpoints whose targets run a recipe over one artifact.

        Three hops — target to deployment to workload to recipe — expressed as a
        subquery so that filtering by model stays one round trip rather than a
        fan-out over every endpoint's target list. The recipe is the workload's:
        a deployment is that workload on one cluster and declares no recipe of
        its own.
        """
        return (
            select(EndpointTarget.endpoint_id)
            .join(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Recipe, Workload.recipe_id == Recipe.id)
            .where(Recipe.artifact_id.in_(ref_ids(Artifact, artifact)))
        )

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        project: str | None = None,
        artifact: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Endpoint.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Endpoint.seq > after_seq)
        if project is not None:
            query = query.where(Endpoint.project_id.in_(ref_ids(Project, project, "slug")))
        if artifact is not None:
            query = query.where(Endpoint.id.in_(self.serving_artifact_query(artifact)))
        if search is not None:
            query = query.where(Endpoint.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Endpoint.labels[key].astext == value)
        return query

    def deployment_uid_query(self, deployment_ids: list) -> Select:
        return select(Deployment.id, Deployment.uid).where(Deployment.id.in_(deployment_ids))

    def resolved_query(self, ref: str | None = None) -> Select:
        """Endpoints joined out to the addresses their targets sit behind.

        An outer join all the way down, deliberately: a target whose cluster has
        declared no serving address still has to come back, because the caller
        has to be able to tell a misconfigured endpoint from an absent one.
        Filtering happens above, where it can say why.

        The serving quota is the sixth entity and joins 1:1 on its unique
        endpoint FK, so it multiplies no rows and the per-endpoint grouping above
        is unaffected. It is here rather than on a surface of its own because the
        gateway enforces it on the request path and already polls this table.
        """
        query = (
            select(Endpoint, EndpointTarget, Deployment, Cluster, ClusterServing, ServingQuota)
            .outerjoin(EndpointTarget, EndpointTarget.endpoint_id == Endpoint.id)
            .outerjoin(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .outerjoin(Cluster, Deployment.cluster_id == Cluster.id)
            .outerjoin(ClusterServing, ClusterServing.cluster_id == Cluster.id)
            .outerjoin(ServingQuota, ServingQuota.endpoint_id == Endpoint.id)
            .where(Endpoint.enabled.is_(True))
            .order_by(Endpoint.seq.asc())
        )
        if ref is not None:
            column = Endpoint.uid if is_uid(ref) else Endpoint.name
            query = query.where(column == ref)
        return query

    async def resolved(self, ref: str | None = None) -> list[Row]:
        result = await self.db.execute(self.resolved_query(ref))
        return list(result.all())

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.unique().all())

    async def deployment_uids(self, deployment_ids: list) -> dict[UUID, str]:
        """One statement for a whole page of targets, never one per endpoint."""
        if not deployment_ids:
            return {}
        result = await self.db.execute(self.deployment_uid_query(deployment_ids))
        return {row.id: row.uid for row in result.all()}

    async def project_by_ref(self, ref: str) -> Project | None:
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def deployments_by_ref(self, refs: list[str]) -> dict[str, Deployment]:
        """Resolves target references, keyed by the reference the caller used.

        A deployment name is unique only within a cluster, so a bare name can
        name more than one row. Endpoints are global and have no cluster to
        narrow it with, which is why an ambiguous name is the caller's problem to
        avoid by passing an id — the oldest match wins otherwise.
        """
        found: dict[str, Deployment] = {}
        for ref in refs:
            column = Deployment.uid if is_uid(ref) else Deployment.name
            result = await self.db.execute(
                select(Deployment).where(column == ref).order_by(Deployment.seq.asc()).limit(1)
            )
            deployment = result.scalar_one_or_none()
            if deployment is not None:
                found[ref] = deployment
        return found

    async def add(self, endpoint: Endpoint) -> Endpoint:
        self.db.add(endpoint)
        await self.db.flush()
        return endpoint

    async def delete(self, endpoint: Endpoint) -> None:
        await self.db.delete(endpoint)
        await self.db.flush()
