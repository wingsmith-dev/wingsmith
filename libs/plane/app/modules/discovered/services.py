import datetime as dt

from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import WORKLOAD
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import DiscoveredWorkload
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.workloads.schemas import WorkloadSchema
from app.modules.workloads.services import WorkloadService
from app.modules.deployments.services import OBSERVED

from .derive import RecipeDraft
from .derive import derive
from .schemas import PromoteRequest
from .schemas import DecisionRequest
from .schemas import DiscoveredWorkloadSchema
from .repository import DISMISSED
from .repository import DiscoveredWorkloadRepository


TRACKED = "tracked"
IGNORED = "ignored"
PROMOTED = "promoted"
NEW = "new"

STATUSES = frozenset({NEW, TRACKED, IGNORED, DISMISSED, PROMOTED})


class DiscoveredWorkloadService:
    """Sightings in, decisions out.

    The service has no method that declares anything. Tracking a discovered
    workload writes one row in `demand_workload_adoption` and stops there: it
    creates no `Deployment`, resolves no recipe, and produces nothing that
    `stream_desired` will ever read. That is deliberate and is the reason this
    module exists rather than an "import to deployment" endpoint — the plane
    cannot cause a rollout of a workload it has declared nothing about, and a
    model whose weights are already resident stays resident.
    """

    def __init__(self, db: AsyncSession, principal: str | None = None) -> None:
        self.db = db
        self.principal = principal
        self.repo = DiscoveredWorkloadRepository(db)

    def schema(self, row: Row) -> DiscoveredWorkloadSchema:
        workload: DiscoveredWorkload = row[0]
        adoption = row[3]
        deployment_uid = row[5]
        return DiscoveredWorkloadSchema(
            id=workload.uid,
            cluster_id=row[1],
            cluster_name=row[2],
            namespace=workload.namespace,
            name=workload.name,
            kind=workload.kind,
            model=workload.model,
            images=list(workload.images),
            replicas_desired=workload.replicas_desired,
            replicas_ready=workload.replicas_ready,
            devices_per_replica=workload.devices_per_replica,
            devices_total=workload.devices_total,
            node_ids=list(workload.node_uids),
            labels=workload.labels,
            status=self._status(adoption, deployment_uid),
            deployment_id=deployment_uid,
            spec_fingerprint=workload.spec_fingerprint,
            project_id=row[4],
            note=adoption.note if adoption is not None else None,
            decided_by=adoption.decided_by if adoption is not None else None,
            decided_at=adoption.decided_at if adoption is not None else None,
            first_seen_at=workload.first_seen_at,
            observed_at=workload.observed_at,
        )

    @staticmethod
    def _status(adoption, deployment_uid: str | None) -> str:
        """Promotion outranks the decision that led to it.

        A promoted workload is necessarily tracked, and reporting it as `tracked`
        would hide the one thing about it that changed: the plane now holds a
        deployment for it.
        """
        if deployment_uid is not None:
            return PROMOTED
        return adoption.decision if adoption is not None else NEW

    async def _one(self, ref: str, cluster: str | None = None) -> Row:
        row = await self.repo.get(ref, cluster)
        if row is None:
            raise NotFound(f"no discovered workload {ref!r}")
        return row

    async def get(self, ref: str, cluster: str | None = None) -> DiscoveredWorkloadSchema:
        return self.schema(await self._one(ref, cluster))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> ListResponse[DiscoveredWorkloadSchema]:
        if status is not None and status not in STATUSES:
            raise Invalid(f"unknown status {status!r}", {"allowed": sorted(STATUSES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            namespace=namespace,
            status=status,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(row) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def decide(self, ref: str, decision: str, data: DecisionRequest) -> DiscoveredWorkloadSchema:
        """Records what we decided about a sighting and nothing else."""
        row = await self._one(ref)
        workload: DiscoveredWorkload = row[0]

        project_id = None
        if data.project is not None:
            found = await self.repo.project_by_ref(data.project)
            if found is None:
                raise NotFound(f"no project {data.project!r}")
            project_id = found[0]

        await self.repo.decide(
            {
                "cluster_id": workload.cluster_id,
                "workload_uid": workload.uid,
                "decision": decision,
                "project_id": project_id,
                "note": data.note,
                "decided_by": self.principal,
                "decided_at": dt.datetime.now(dt.UTC),
            }
        )
        await self.db.commit()
        return await self.get(workload.uid)

    async def dismiss(self, ref: str, data: DecisionRequest) -> None:
        """Takes a sighting off the list for good, without touching the sighting.

        The row is not deleted and could not be: it is observed state, ingestion is
        its only writer, and a row deleted here would be written straight back on
        the next observation because the workload is still running. What is
        recorded instead is the decision, and the listing honours it — which is
        also what makes it reversible, where a delete racing an observation would
        not be.

        Refused once the sighting has been promoted. There is a deployment
        pointing at it by then, and hiding what it adopted would leave nobody able
        to see what the deployment took ownership of.
        """
        row = await self._one(ref)
        workload: DiscoveredWorkload = row[0]
        if row[5] is not None:
            raise Conflict(
                f"{workload.name!r} was promoted and is managed as a deployment now",
                {"deployment": row[5]},
            )
        await self.repo.decide(
            {
                "cluster_id": workload.cluster_id,
                "workload_uid": workload.uid,
                "decision": DISMISSED,
                "project_id": None,
                "note": data.note,
                "decided_by": self.principal,
                "decided_at": dt.datetime.now(dt.UTC),
            }
        )
        await self.db.commit()

    async def restore(self, ref: str) -> DiscoveredWorkloadSchema:
        """Undoes a dismissal, so the workload is listed again as it was.

        The decision is dropped rather than replaced. A dismissal that had been
        recorded as `new` would be a decision nobody made, and the sighting has
        been sitting there the whole time — putting it back means having no
        opinion about it again.
        """
        row = await self._one(ref)
        workload: DiscoveredWorkload = row[0]
        adoption = row[3]
        if adoption is None or adoption.decision != DISMISSED:
            raise Invalid(f"{workload.name!r} was not dismissed", {"status": self._status(adoption, row[5])})
        await self.repo.undecide(workload.uid)
        await self.db.commit()
        return await self.get(workload.uid)

    async def recipe_draft(self, ref: str, cluster: str | None = None) -> RecipeDraft:
        """Reads a sighting as a proposed recipe. Writes nothing.

        A read rather than a POST because it is one: the same sighting derives the
        same draft every time, and nothing about asking the question changes the
        answer. What a person does with the draft — edit it, save it as a recipe,
        throw it away — happens on the recipes module's own endpoints, so there is
        one path that creates a recipe rather than two that must agree.
        """
        row = await self._one(ref, cluster=cluster)
        workload: DiscoveredWorkload = row[0]
        draft = derive(
            spec=workload.spec or {},
            labels=workload.labels or {},
            name=workload.name,
            devices_per_replica=workload.devices_per_replica,
            device_class=await self.repo.device_class_of(list(workload.node_uids or [])),
        )
        draft.artifact.existing_id = await self.repo.artifact_by_reference(
            draft.artifact.digest, draft.artifact.source_uri
        )
        return draft

    async def promote(self, ref: str, data: PromoteRequest) -> WorkloadSchema:
        """Turns a sighting into a workload the plane declares, or refuses to.

        The pair is minted together: one Workload pinned to the cluster the sighting
        is running in, and its one Deployment there. Pinned and capped at one cluster
        because an adopted object is a record of something that already exists in one
        place — expanding it elsewhere would be starting a second copy, which is the
        opposite of cataloguing.

        This is the one call in this module that writes declared state, and every
        guard on it exists because the failure mode is a restart of a model that is
        already serving.

        The digest is checked twice over. `data.fingerprint` must be what the
        sighting currently reports, so a caller cannot promote a workload that
        changed between reading and deciding; and the digest is then recorded on
        the deployment, so the cluster refuses to reconcile it if it drifts later.
        The second check is the load-bearing one — the sighting can be up to one
        observation interval stale, and the cluster is the only thing that knows
        what is running right now.

        Scale comes from the workload rather than the caller. A promotion that also
        set the replica count would scale a live workload as a side effect of
        cataloguing it.
        """
        row = await self._one(ref)
        workload: DiscoveredWorkload = row[0]
        if row[5] is not None:
            raise Conflict(
                f"{workload.name!r} was already promoted",
                {"deployment": row[5]},
            )
        if not workload.spec_fingerprint:
            raise Invalid(
                "this cluster has not reported what the workload is running, so there is "
                "nothing to pin the adoption to",
                {"workload": workload.uid},
            )
        if data.fingerprint != workload.spec_fingerprint:
            raise Conflict(
                "the workload changed since it was read; re-read it and promote again",
                {"observed": workload.spec_fingerprint, "sent": data.fingerprint},
            )

        recipe = await self.repo.recipe_by_ref(data.recipe)
        if recipe is None:
            raise NotFound(f"no recipe {data.recipe!r}")
        project = await self.repo.project_row(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority_class_id = await self._priority_class_id(data.priority_class, project)

        replicas = max(workload.replicas_desired, 0)
        now = dt.datetime.now(dt.UTC)
        name = data.name or workload.name
        deployment_workload = Workload(
            uid=new_uid(WORKLOAD),
            name=name,
            kind="service",
            project_id=project.id,
            recipe_id=recipe[0],
            priority_class_id=priority_class_id,
            pinned_cluster_id=workload.cluster_id,
            max_clusters=1,
            strategy="pack",
            min_scale=replicas,
            max_scale=replicas,
            desired_scale=replicas,
            gang_size=1,
            preemptible=False,
            paused=False,
            adapter_artifact_ids=[],
            overrides={},
            labels=dict(workload.labels or {}),
        )
        deployment = Deployment(
            uid=new_uid(DEPLOYMENT),
            workload=deployment_workload,
            cluster_id=workload.cluster_id,
            name=name,
            min_replicas=replicas,
            max_replicas=replicas,
            desired_replicas=replicas,
            admission_phase="running",
            admission_reason=f"adopted from what {workload.uid} is already running",
            queued_at=now,
            admitted_at=now,
            placed_at=now,
            generation=1,
            enabled=True,
            adopted_workload_uid=workload.uid,
            adopted_fingerprint=workload.spec_fingerprint,
            authority=OBSERVED,
        )
        try:
            await self.repo.add(deployment_workload, deployment)
            await self.repo.decide(
                {
                    "cluster_id": workload.cluster_id,
                    "workload_uid": workload.uid,
                    "decision": TRACKED,
                    "project_id": project.id,
                    "note": None,
                    "decided_by": self.principal,
                    "decided_at": dt.datetime.now(dt.UTC),
                }
            )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"a workload named {name!r} already exists in this project") from exc
        return await WorkloadService(self.db).get(deployment_workload.uid)

    async def _priority_class_id(self, ref: str | None, project):
        """The project's default answers when the caller does not, as elsewhere."""
        if ref is not None:
            found = await self.repo.priority_class_by_name(ref)
            if found is None:
                raise NotFound(f"no priority class {ref!r}")
            return found.id
        if project.default_priority_class_id is not None:
            return project.default_priority_class_id
        fallback = await self.repo.default_priority_class()
        if fallback is None:
            raise Invalid("no priority class was given and none is marked default")
        return fallback.id
