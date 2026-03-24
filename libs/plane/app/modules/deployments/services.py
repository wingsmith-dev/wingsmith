from __future__ import annotations

import datetime as dt

from uuid import UUID

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import WORKLOAD
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Workload
from app.db.models import Deployment
from app.db.notify import DEPLOYMENT as DEPLOYMENT_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import DeploymentSchema
from .schemas import DeploymentCreateRequest
from .schemas import DeploymentUpdateRequest
from .schemas import DeploymentWorkloadSchema
from .schemas import DeploymentReplicaRollupSchema
from .repository import DeploymentRepository


TYPES = frozenset({"service", "task", "gang"})
PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})

EMPTY = DeploymentReplicaRollupSchema(
    total=0,
    ready=0,
    running=0,
    pending=0,
    failed=0,
    at_current_generation=0,
)

RECONCILED_FIELDS = frozenset({"adapter_artifacts", "overrides"})


def check_scale(min_replicas: int, desired_replicas: int, max_replicas: int) -> None:
    """Rejects scale bounds that contradict each other.

    Zero is a legitimate floor — scaling a deployment to nothing is how it is
    parked without being deleted — so the only rules are non-negativity and the
    ordering of the three numbers.
    """
    for name, value in (
        ("min_replicas", min_replicas),
        ("desired_replicas", desired_replicas),
        ("max_replicas", max_replicas),
    ):
        if value < 0:
            raise Invalid(f"{name} cannot be negative", {name: value})
    if max_replicas < min_replicas:
        raise Invalid(
            "max_replicas is below min_replicas",
            {"min_replicas": min_replicas, "max_replicas": max_replicas},
        )
    if not min_replicas <= desired_replicas <= max_replicas:
        raise Invalid(
            "desired_replicas is outside the declared bounds",
            {
                "min_replicas": min_replicas,
                "desired_replicas": desired_replicas,
                "max_replicas": max_replicas,
            },
        )


def _revision(deployment: Deployment, workload: Workload) -> str:
    """The value `If-Match` is checked against, across both halves of the object.

    A deployment is two rows, and a patch that only moves priority touches the
    workload and leaves `demand_deployment.updated_at` where it was. Deriving the
    revision from the deployment alone would let that write go unnoticed by the
    next conditional update, so the later of the two stamps wins.
    """
    stamps = [
        deployment.updated_at or deployment.created_at,
        workload.updated_at or workload.created_at,
    ]
    return str(int(max(stamps).timestamp() * 1_000_000))


def _rollup(row: dict | None) -> DeploymentReplicaRollupSchema:
    if not row:
        return EMPTY
    return DeploymentReplicaRollupSchema(
        total=row.get("total", 0),
        ready=row.get("ready", 0),
        running=row.get("running", 0),
        pending=row.get("pending", 0),
        failed=row.get("failed", 0),
        at_current_generation=row.get("at_current_generation", 0),
    )


class DeploymentService:
    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = DeploymentRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _announce(self, cluster_uid: str, uid: str, generation: int) -> None:
        """Wakes whichever hub holds this cluster's stream, after the write is committed.

        Every caller of this is on the far side of its own `commit()`, and that
        ordering is the contract: the hub answers the hint by re-reading the row,
        so announcing first would race the reader against a transaction that has
        not landed. Missing the wakeup entirely costs latency and nothing else —
        the next full resync carries the same state — which is why this never
        stands between the caller and its response.
        """
        await self.notifier.changed(
            self.db,
            DeclaredChange(cluster_id=cluster_uid, kind=DEPLOYMENT_KIND, uid=uid, generation=generation),
        )

    def schema(
        self,
        deployment: Deployment,
        workload: Workload,
        cluster_uid: str,
        recipe_uid: str,
        project_uid: str,
        priority_class: str,
        adapter_uids: list[str],
        replicas: DeploymentReplicaRollupSchema,
    ) -> DeploymentSchema:
        return DeploymentSchema(
            id=deployment.uid,
            name=deployment.name,
            cluster_id=cluster_uid,
            recipe_id=recipe_uid,
            min_replicas=deployment.min_replicas,
            max_replicas=deployment.max_replicas,
            desired_replicas=deployment.desired_replicas,
            generation=deployment.generation,
            enabled=deployment.enabled,
            adapter_artifact_ids=adapter_uids,
            overrides=deployment.overrides,
            labels=deployment.labels,
            created_at=deployment.created_at,
            updated_at=deployment.updated_at,
            revision=_revision(deployment, workload),
            adopted_workload_id=deployment.adopted_workload_uid,
            adopted_fingerprint=deployment.adopted_fingerprint,
            workload=DeploymentWorkloadSchema(
                type=workload.kind,
                phase=workload.admission_phase,
                reason=workload.admission_reason,
                project_id=project_uid,
                priority_class=priority_class,
                preemptible=workload.preemptible,
                gang_size=workload.gang_size,
                queued_at=workload.queued_at,
                admitted_at=workload.admitted_at,
                placed_at=workload.placed_at,
                started_at=workload.started_at,
                deadline_at=workload.deadline_at,
            ),
            replicas=replicas,
        )

    async def _one(self, ref: str, cluster: str | None = None):
        row = await self.repo.get(ref, cluster)
        if row is None:
            raise NotFound(f"no deployment {ref!r}")
        return row

    async def _render(self, row, replicas: DeploymentReplicaRollupSchema | None = None) -> DeploymentSchema:
        deployment, workload = row[0], row[1]
        if replicas is None:
            rollup = await self.repo.replicas([deployment.id])
            replicas = _rollup(rollup.get(deployment.id))
        adapters = await self.repo.adapter_uids(list(deployment.adapter_artifact_ids))
        return self.schema(
            deployment=deployment,
            workload=workload,
            cluster_uid=row.cluster_uid,
            recipe_uid=row.recipe_uid,
            project_uid=row.project_uid,
            priority_class=row.priority_class,
            adapter_uids=[adapters[i] for i in deployment.adapter_artifact_ids if i in adapters],
            replicas=replicas,
        )

    async def get(self, ref: str, cluster: str | None = None) -> DeploymentSchema:
        return await self._render(await self._one(ref, cluster))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        recipe: str | None = None,
        project: str | None = None,
        phase: str | None = None,
        type: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[DeploymentSchema]:
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        if type is not None and type not in TYPES:
            raise Invalid(f"unknown type {type!r}", {"allowed": sorted(TYPES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            recipe=recipe,
            project=project,
            phase=phase,
            type=type,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        rollup = await self.repo.replicas([r[0].id for r in kept])
        adapters = await self.repo.adapter_uids(
            [i for r in kept for i in r[0].adapter_artifact_ids]
        )
        items = [
            self.schema(
                deployment=r[0],
                workload=r[1],
                cluster_uid=r.cluster_uid,
                recipe_uid=r.recipe_uid,
                project_uid=r.project_uid,
                priority_class=r.priority_class,
                adapter_uids=[adapters[i] for i in r[0].adapter_artifact_ids if i in adapters],
                replicas=_rollup(rollup.get(r[0].id)),
            )
            for r in kept
        ]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def _adapter_ids(self, refs: list[str]) -> list[UUID]:
        if not refs:
            return []
        found = await self.repo.artifacts_by_ref(refs)
        if len(found) != len(refs):
            names = {a.uid for a in found} | {a.name for a in found}
            raise NotFound("unknown adapter artifact", {"missing": sorted(set(refs) - names)})
        return [a.id for a in found]

    async def _priority_class_id(self, ref: str | None, project) -> UUID:
        """Resolves the priority class, falling back the way a scheduler must.

        Every workload carries one from day one, so an absent reference is not an
        absent priority: the project's default answers first, then the system
        default, and only a system with neither is a real error.
        """
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

    async def create(self, data: DeploymentCreateRequest) -> DeploymentSchema:
        if data.type not in TYPES:
            raise Invalid(f"unknown type {data.type!r}", {"allowed": sorted(TYPES)})
        if data.gang_size < 1:
            raise Invalid("gang_size must be at least 1", {"gang_size": data.gang_size})
        check_scale(data.min_replicas, data.desired_replicas, data.max_replicas)

        cluster = await self.repo.cluster_by_ref(data.cluster)
        if cluster is None:
            raise NotFound(f"no cluster {data.cluster!r}")
        recipe = await self.repo.recipe_by_ref(data.recipe)
        if recipe is None:
            raise NotFound(f"no recipe {data.recipe!r}")
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority_class_id = await self._priority_class_id(data.priority_class, project)

        workload = Workload(
            uid=new_uid(WORKLOAD),
            kind=data.type,
            project_id=project.id,
            cluster_id=cluster.id,
            priority_class_id=priority_class_id,
            admission_phase="pending",
            gang_size=data.gang_size,
            preemptible=data.preemptible,
            deadline_at=data.deadline_at,
            queued_at=dt.datetime.now(dt.UTC),
        )
        deployment = Deployment(
            uid=new_uid(DEPLOYMENT),
            workload=workload,
            recipe_id=recipe.id,
            cluster_id=cluster.id,
            name=data.name,
            min_replicas=data.min_replicas,
            max_replicas=data.max_replicas,
            desired_replicas=data.desired_replicas,
            generation=1,
            enabled=data.enabled,
            adapter_artifact_ids=await self._adapter_ids(data.adapter_artifacts),
            overrides=data.overrides,
            labels=data.labels,
        )
        try:
            await self.repo.add(workload, deployment)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"deployment {data.name!r} already exists in {data.cluster!r}") from exc
        await self._announce(cluster.uid, deployment.uid, deployment.generation)
        return await self.get(deployment.uid)

    async def update(self, ref: str, data: DeploymentUpdateRequest, if_match: str | None = None) -> DeploymentSchema:
        row = await self._one(ref)
        deployment, workload = row[0], row[1]
        revision = _revision(deployment, workload)
        if if_match is not None and if_match != revision:
            raise Conflict("deployment changed since it was read", {"revision": revision})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        check_scale(
            fields.get("min_replicas", deployment.min_replicas),
            fields.get("desired_replicas", deployment.desired_replicas),
            fields.get("max_replicas", deployment.max_replicas),
        )

        if "priority_class" in fields:
            name = fields.pop("priority_class")
            found = await self.repo.priority_class_by_name(name)
            if found is None:
                raise NotFound(f"no priority class {name!r}")
            workload.priority_class_id = found.id
        for key in ("preemptible", "deadline_at"):
            if key in fields:
                setattr(workload, key, fields.pop(key))

        if "adapter_artifacts" in fields:
            deployment.adapter_artifact_ids = await self._adapter_ids(fields.pop("adapter_artifacts"))
        bumped = any(k in RECONCILED_FIELDS for k in data.to_dict(exclude_unset=True))
        for key, value in fields.items():
            setattr(deployment, key, value)
        if bumped:
            deployment.generation = deployment.generation + 1

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("deployment conflicts with an existing one") from exc
        await self._announce(row.cluster_uid, deployment.uid, deployment.generation)
        return await self.get(deployment.uid)

    async def delete(self, ref: str) -> None:
        """Deletes the workload, which the deployment row cascades off.

        Replicas cascade too, and that is correct: they are the cluster's report
        about a thing that no longer exists. Ingestion writes them again if the
        cluster has not caught up yet, which is a reconciliation lag rather than
        a resurrection.
        """
        row = await self._one(ref)
        uid, generation, cluster_uid = row[0].uid, row[0].generation, row.cluster_uid
        await self.repo.delete(row[1], row[0])
        await self.db.commit()
        await self._announce(cluster_uid, uid, generation)
