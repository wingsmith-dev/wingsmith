from __future__ import annotations

import builtins

from uuid import UUID

from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import ColumnElement
from sqlalchemy import or_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Tenant
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import ServingQuota
from app.db.models import EndpointAlias
from app.db.models import ClusterServing
from app.db.models import EndpointTarget
from app.core.lookup import at_most_one
from app.core.filters import ref_ids
from app.modules.artifacts.scope import visible
from app.modules.artifacts.scope import visible_ids


def ref_query(ref: str, project: str | None = None) -> Select:
    """The endpoint ids a reference names: an id, or a name that is unique only within its project."""
    column = Endpoint.uid if is_uid(ref) else Endpoint.name
    query = select(Endpoint.id).where(column == ref)
    if project is not None:
        query = query.where(Endpoint.project_id.in_(ref_ids(Project, project, "slug")))
    return query


AMBIGUOUS_HINT = "say which with `project`"


def target_deployment() -> ColumnElement[bool]:
    """The join condition from an `EndpointTarget` to the deployments it routes to: its pinned
    deployment, or every deployment of the workload a workload ref names in the endpoint's project
    (ADR 0026). The enclosing query has to carry `Endpoint` and `EndpointTarget` in its FROM."""
    ref = aliased(Workload, name="ref_workload")
    by_ref = (
        select(ref.id)
        .where(ref.project_id == Endpoint.project_id, ref.name == EndpointTarget.workload_name)
        .correlate(Endpoint, EndpointTarget)
    )
    return or_(Deployment.id == EndpointTarget.deployment_id, Deployment.workload_id.in_(by_ref))


PUBLIC_ID_LOCK_PREFIX = "wingsmith.endpoint-id:"


async def endpoint_by_ref(db: AsyncSession, ref: str, project: str | None = None) -> Endpoint | None:
    """The one endpoint a reference names, None when none, a 400 when a bare name is in several projects."""
    query = select(Endpoint).where(Endpoint.id.in_(ref_query(ref, project))).limit(2)
    found = (await db.execute(query)).scalars().all()
    return at_most_one(found, "endpoint", ref, AMBIGUOUS_HINT)


class EndpointRepository:
    """Reads endpoints and the deployments behind them. No cluster ever appears.

    The only join out of this module goes through `EndpointTarget` to a
    deployment, and it exists to answer "which endpoints serve these weights",
    not to give the endpoint a location.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Endpoint, Project.uid.label("project_uid"))
            .join(Project, Endpoint.project_id == Project.id)
            .options(selectinload(Endpoint.targets), selectinload(Endpoint.aliases))
        )

    def get_query(self, ref: str, project: str | None = None) -> Select:
        """An id, or a name that is unique only within its project."""
        return self._base().where(Endpoint.id.in_(ref_query(ref, project)))

    def serving_artifact_query(self, artifact: str, project=None) -> Select:
        """The endpoints whose targets serve one artifact.

        Two hops (target to deployment to workload), expressed as a subquery so
        that filtering by model stays one round trip instead of a fan-out over
        every endpoint's target list. The artifact is the workload's: a
        deployment is that workload on one cluster and names no artifact of its
        own.
        """
        return (
            select(EndpointTarget.endpoint_id)
            .join(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .join(Workload, Deployment.workload_id == Workload.id)
            .where(Workload.artifact_id.in_(visible_ids(artifact, project)))
        )

    def serving_workload_query(self, workload: str) -> Select:
        """The endpoints whose targets point at a workload's deployments.

        The reverse of "which deployments does this endpoint route to": one hop
        from target to deployment, filtered to the deployments of the named
        workload, expressed as a subquery so the reverse index stays one round
        trip instead of a fan-out over every endpoint's target list.
        """
        by_deployment = (
            select(EndpointTarget.endpoint_id)
            .join(Deployment, EndpointTarget.deployment_id == Deployment.id)
            .where(Deployment.workload_id.in_(ref_ids(Workload, workload)))
        )
        by_name = (
            select(EndpointTarget.endpoint_id)
            .join(Endpoint, Endpoint.id == EndpointTarget.endpoint_id)
            .join(
                Workload, (Workload.project_id == Endpoint.project_id) & (Workload.name == EndpointTarget.workload_name)
            )
            .where(Workload.id.in_(ref_ids(Workload, workload)))
        )
        return by_deployment.union(by_name)

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        project: str | None = None,
        artifact: str | None = None,
        workload: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Select:
        query = self._base().order_by(Endpoint.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Endpoint.seq > after_seq)
        if project is not None:
            query = query.where(Endpoint.project_id.in_(ref_ids(Project, project, "slug")))
        if artifact is not None:
            scope = None if project is None else ref_ids(Project, project, "slug")
            query = query.where(Endpoint.id.in_(self.serving_artifact_query(artifact, scope)))
        if workload is not None:
            query = query.where(Endpoint.id.in_(self.serving_workload_query(workload)))
        if search is not None:
            query = query.where(Endpoint.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Endpoint.labels[key].astext == value)
        return query

    def deployment_uid_query(self, deployment_ids: list) -> Select:
        return select(Deployment.id, Deployment.uid).where(Deployment.id.in_(deployment_ids))

    def resolved_query(self, endpoint: UUID | None = None) -> Select:
        """Endpoints joined out to the addresses their targets sit behind.

        An outer join all the way down, by design: a target whose cluster has
        declared no serving address still has to come back, because the caller
        has to be able to tell a misconfigured endpoint from an absent one.
        Filtering happens above, where it can say why.

        The serving quota is the sixth entity and joins 1:1 on its unique
        endpoint FK, so it multiplies no rows and the per-endpoint grouping above
        is unaffected. It is here, not on a surface of its own because the
        gateway already polls this table, so it is where enforcement will read it.
        The gateway does not enforce serving quotas yet.

        The workload and artifact come along for one reason: the name the engine
        answers to. A client calls an endpoint by its public name, and the
        frontend behind it serves under whatever the workload declares, or,
        when it declares nothing, under the artifact's Hugging Face slug. The
        gateway rewrites one to the other, so the table has to carry both. Both
        hops are NOT NULL FKs and 1:1, so they multiply no rows; they are outer
        joins only because everything below `EndpointTarget` already is.

        A target that names a workload (ADR 0026) joins that workload by name inside the
        endpoint's own project, never another, and then every deployment of it: one row per
        deployment, which is the expansion. A name with no such workload yields one row with no
        deployment, the same shape as a target whose deployment is gone.

        The workload also carries `serving_digest`, which is what the resolution's
        revision is versioned by; see `_revision_of`.

        `Project` and `Tenant` are the last two entities, joined inward,
        not out: `Endpoint.project_id` and `Project.tenant_id` are both NOT
        NULL and 1:1, so, like the workload and artifact hops, they multiply
        no rows. They carry `tenant_id` onto the resolved endpoint, which is
        what lets the gateway answer only for the tenant a request belongs to
        (docs/decisions/0016); `Project`'s own stamp goes into the revision so a
        project's tenant reassignment is not silently missed by a poller
        holding a `304`. A disabled project's endpoints are left out, the same as
        a disabled endpoint: the gateway's table carries no project flag, so this
        is the only place a project can be switched off.
        """
        query = (
            select(
                Endpoint,
                EndpointTarget,
                Deployment,
                Cluster,
                ClusterServing,
                ServingQuota,
                Artifact,
                Workload,
                Project,
                Tenant,
            )
            .join(Project, Project.id == Endpoint.project_id)
            .join(Tenant, Tenant.id == Project.tenant_id)
            .outerjoin(EndpointTarget, EndpointTarget.endpoint_id == Endpoint.id)
            .outerjoin(Deployment, target_deployment())
            .outerjoin(Cluster, Deployment.cluster_id == Cluster.id)
            .outerjoin(ClusterServing, ClusterServing.cluster_id == Cluster.id)
            .outerjoin(ServingQuota, ServingQuota.endpoint_id == Endpoint.id)
            .outerjoin(Workload, Deployment.workload_id == Workload.id)
            # Visible to the endpoint's project: a target serving another project's private weights resolves to no artifact and is not routed.
            .outerjoin(Artifact, (Workload.artifact_id == Artifact.id) & visible(Endpoint.project_id))
            .options(selectinload(Endpoint.aliases))
            .where(Endpoint.enabled.is_(True), Project.enabled.is_(True))
            .order_by(Endpoint.seq.asc(), Deployment.uid.asc())
        )
        if endpoint is not None:
            query = query.where(Endpoint.id == endpoint)
        return query

    async def resolved(self, ref: UUID | None = None) -> builtins.list[Row]:
        result = await self.db.execute(self.resolved_query(ref))
        return list(result.all())

    async def matches(self, ref: str, project: str | None = None, lock: bool = False) -> builtins.list[Row]:
        """Every endpoint a reference could mean, capped at two so "one" and "several" differ.

        `lock` holds the row until the transaction ends, re-read, so an `If-Match` compared under it holds.
        """
        query = self.get_query(ref, project).limit(2)
        if lock:
            query = query.with_for_update(of=Endpoint).execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return list(result.all())

    async def candidates(self, name: str) -> builtins.list[Row]:
        """Every endpoint answering to a bare name, with its project, for the ambiguity error."""
        result = await self.db.execute(
            select(Endpoint.uid, Project.slug)
            .join(Project, Endpoint.project_id == Project.id)
            .where(Endpoint.name == name)
        )
        return list(result.all())

    async def list(self, **kwargs) -> builtins.list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.unique().all())

    async def deployment_uids(self, deployment_ids: list) -> dict[UUID, str]:
        """One statement for a whole page of targets, never one per endpoint."""
        if not deployment_ids:
            return {}
        result = await self.db.execute(self.deployment_uid_query(deployment_ids))
        return {row.id: row.uid for row in result.all()}

    async def existing_workloads(self, pairs: set[tuple[UUID, str]]) -> set[tuple[UUID, str]]:
        """Which (project id, workload name) references name a workload, in one statement."""
        if not pairs:
            return set()
        names = {name for _, name in pairs}
        projects = {project for project, _ in pairs}
        result = await self.db.execute(
            select(Workload.project_id, Workload.name).where(
                Workload.name.in_(names), Workload.project_id.in_(projects)
            )
        )
        return {(row.project_id, row.name) for row in result.all()} & pairs

    async def project_by_ref(self, ref: str) -> Project | None:
        """By id or by slug. A slug is unique per tenant, so one two tenants share is refused."""
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(2))
        return at_most_one(list(result.scalars()), "project", ref)

    async def deployments_by_ref(
        self, refs: builtins.list[str], tenant_id: UUID | None, project_id: UUID
    ) -> dict[str, builtins.list[Deployment]]:
        """Every deployment each target reference could mean, inside one tenant, in one query.

        Scoped to the tenant that owns the endpoint: an endpoint routes to deployments
        of its own tenant's workloads, so a reference to anyone else's is not found,
        not quietly routed to. A bare name is a workload name, which projects may share,
        so it resolves inside the endpoint's own project, as does an object name (what a manifest
        writes); an id reaches across the tenant.
        One name still matches a deployment per cluster; the caller refuses that instead
        of choosing one. A deployment serving another project's private artifact is not
        found either: routing to it would expose those weights outside their project.
        """
        if not refs:
            return {}
        uids = [ref for ref in refs if is_uid(ref)]
        names = [ref for ref in refs if not is_uid(ref)]
        query = (
            select(Deployment)
            .join(Workload, Deployment.workload_id == Workload.id)
            .join(Project, Workload.project_id == Project.id)
            .join(Artifact, Workload.artifact_id == Artifact.id)
            .where(
                Deployment.uid.in_(uids)
                | (
                    (Deployment.name.in_(names) | Deployment.object_name.in_(names))
                    & (Workload.project_id == project_id)
                )
            )
            .where(visible(project_id))
            .order_by(Deployment.seq.asc())
        )
        if tenant_id is not None:
            query = query.where(Project.tenant_id == tenant_id)
        rows = (await self.db.execute(query)).scalars().all()
        found: dict[str, list[Deployment]] = {}
        for deployment in rows:
            for ref in {deployment.uid, deployment.name, deployment.object_name}:
                if ref in refs:
                    found.setdefault(ref, []).append(deployment)
        return found

    async def tenant_of(self, project_id: UUID) -> UUID | None:
        return await self.db.scalar(select(Project.tenant_id).where(Project.id == project_id))

    async def artifact_slug(self, deployment_id: UUID) -> str:
        """The model id the weights behind one deployment answer to.

        Read so that an endpoint created over a deployment can default its own slug to it, which
        is what makes the two agree without anybody typing the model's name twice. The join is
        deployment -> workload -> artifact, because the deployment carries no identity of its own:
        it is a placement of a workload, and what is being served is a property of the weights.
        """
        result = await self.db.execute(
            select(Artifact.slug)
            .join(Workload, (Workload.artifact_id == Artifact.id) & visible(Workload.project_id))
            .join(Deployment, Deployment.workload_id == Workload.id)
            .where(Deployment.id == deployment_id)
            .limit(1)
        )
        return (result.scalar_one_or_none() or "").strip()

    async def workload_artifact_slug(self, project_id: UUID, name: str) -> str:
        """`artifact_slug` for a workload ref: empty when the workload does not exist yet."""
        result = await self.db.execute(
            select(Artifact.slug)
            .join(Workload, (Workload.artifact_id == Artifact.id) & visible(Workload.project_id))
            .where(Workload.project_id == project_id, Workload.name == name)
            .limit(1)
        )
        return (result.scalar_one_or_none() or "").strip()

    def alias_owner_query(self, alias: str) -> Select:
        return select(EndpointAlias.endpoint_id).where(EndpointAlias.alias == alias)

    def slug_owner_query(self, slug: str) -> Select:
        return select(Endpoint.id).where(Endpoint.slug == slug)

    async def get_alias_owner(self, alias: str) -> UUID | None:
        """Which endpoint already answers to this alias, if any.

        The unique index makes at most one, and a `None` here is the only reason the service is
        allowed to write the row: the constraint is the backstop, not the check, because the
        message a caller needs names the endpoint that holds it.
        """
        result = await self.db.execute(self.alias_owner_query(alias).limit(1))
        return result.scalar_one_or_none()

    async def lock_public_ids(self, ids: builtins.list[str]) -> None:
        """Transaction-scoped advisory locks on public ids, in sorted order so two writers cannot
        deadlock. Released at commit or rollback."""
        for value in sorted(set(ids)):
            key = func.hashtextextended(PUBLIC_ID_LOCK_PREFIX + value, 0)
            await self.db.execute(select(func.pg_advisory_xact_lock(key)))

    async def name_exists(self, slug: str, exclude_id: UUID | None = None) -> bool:
        """Whether any other endpoint is already published under this public id.

        The other half of the one namespace aliases and slugs share. No constraint spans the two
        tables, so an alias shadowing somebody's slug, or a slug moved onto somebody's alias,
        would otherwise be accepted and then resolved by whichever the gateway indexed first.
        """
        query = self.slug_owner_query(slug)
        if exclude_id is not None:
            query = query.where(Endpoint.id != exclude_id)
        result = await self.db.execute(query.limit(1))
        return result.scalar_one_or_none() is not None

    async def replace_aliases(self, endpoint: Endpoint, aliases: builtins.list[str]) -> None:
        """Replaces an endpoint's whole alias set.

        A full replace and never a merge: an alias has no identity beyond its own string, so there
        is nothing to match a patch against and a merge could only ever add.

        Through the relationship, not with a `delete()` statement, and emptied with a flush
        of its own before the new rows are attached: the same two steps, for the same two reasons,
        that `EndpointService.update` uses on `targets`. `alias` is globally unique, so a single
        flush could order an insert before the delete of the row it replaces; and a Core statement
        writes past the identity map, which leaves the session holding the collection it loaded
        before the write and makes the PATCH answer with the set it just replaced.
        """
        endpoint.aliases = []
        await self.db.flush()
        endpoint.aliases = [EndpointAlias(alias=alias) for alias in aliases]
        await self.db.flush()

    async def add(self, endpoint: Endpoint) -> Endpoint:
        self.db.add(endpoint)
        await self.db.flush()
        return endpoint

    async def delete(self, endpoint: Endpoint) -> None:
        await self.db.delete(endpoint)
        await self.db.flush()
