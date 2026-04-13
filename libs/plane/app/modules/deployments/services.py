from __future__ import annotations

import datetime as dt

from uuid import UUID

from msgspec import UNSET
from msgspec import to_builtins
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import WORKLOAD
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import PriorityClass
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
from app.ingestion import statements
from app.modules.placement.services import PlacementService

from .schemas import DeploymentSchema
from .schemas import DeploymentStatusSchema
from .schemas import DeploymentCreateRequest
from .schemas import DeploymentUpdateRequest
from .schemas import DeploymentWorkloadSchema
from .schemas import DeploymentAuthorityRequest
from .schemas import DeploymentReplicaRollupSchema
from .repository import DeploymentRepository


TYPES = frozenset({"service", "task", "gang"})
PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})

OBSERVED = "observed"
MANAGED = "managed"
AUTHORITIES = frozenset({OBSERVED, MANAGED})
"""Who owns a deployment, and the only two answers there are.

Binary on purpose. A deployment with some fields enforced and others left to drift
cannot be reasoned about at the moment somebody needs to, so the object answers in
one word (ADR 0009).
"""

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


def _status(row) -> DeploymentStatusSchema | None:
    """The observed half, or nothing if no cluster has reported on it yet."""
    if row is None:
        return None
    return DeploymentStatusSchema(
        observed_generation=row.observed_generation,
        phase=row.phase,
        reason=row.reason,
        message=row.message,
        desired_replicas=row.desired_replicas,
        ready_replicas=row.ready_replicas,
        updated_replicas=row.updated_replicas,
        unavailable_replicas=row.unavailable_replicas,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        last_error_at=row.last_error_at,
        observed_at=row.observed_at,
    )


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


def _changed(deployment: Deployment, fields: dict, bumped: bool) -> str:
    """A one-line account of what a patch actually moved.

    Written from the applied values rather than the request, so that a field sent
    with the value it already had reads as what it was — nothing.
    """
    if "desired_replicas" in fields:
        return f"{deployment.name} was scaled to {deployment.desired_replicas} replicas"
    if bumped:
        return f"{deployment.name} was changed in a way the cluster has to reconcile"
    return f"{deployment.name} was updated"


class DeploymentService:
    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = DeploymentRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _record(
        self,
        deployment: Deployment,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
    ) -> None:
        """Files what the plane just did to a deployment, in the caller's transaction.

        Before the caller's `commit()` on purpose. The event and the write it
        describes are one fact, and committing them separately would allow the
        pair where a deployment exists and nothing records it being asked for —
        the exact gap that made a deployment's first minutes unreadable.

        This is a declared-state module writing to an observed table, which the
        layout otherwise forbids. It is allowed here because the event is not an
        observation of a cluster: it is the record of an intent this service is
        the only writer of, and `ops_event` is the one timeline a reader should
        have to consult. The distinction is kept visible in the event type —
        `deployment.created` is something a human did, `deployment.placed` is
        something a cluster concluded.
        """
        await self.db.execute(
            statements.deployment_lifecycle(
                deployment.cluster_id,
                deployment.id,
                event,
                message,
                {"deployment_id": deployment.uid, "name": deployment.name, **(details or {})},
                dt.datetime.now(dt.UTC),
                severity,
            )
        )

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
        status: DeploymentStatusSchema | None = None,
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
            authority=deployment.authority,
            requires_single_topology_domain=deployment.requires_single_topology_domain,
            topology_domain_kind=deployment.topology_domain_kind,
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
            status=status,
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
            status=_status(row.DeploymentStatus),
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
        adapters = await self.repo.adapter_uids([i for r in kept for i in r[0].adapter_artifact_ids])
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
                status=_status(r.DeploymentStatus),
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

    async def _cluster(self, data: DeploymentCreateRequest, recipe, project, priority):
        """Which cluster this lands on, and the sentence explaining why.

        Two paths that end in the same place. A caller who named a cluster gets
        it, checked for existence and nothing else — naming one is an override,
        and re-deciding it against capacity would make the override advisory.
        Everyone else gets the scheduler.
        """
        if data.cluster is not None:
            found = await self.repo.cluster_by_ref(data.cluster)
            if found is None:
                raise NotFound(f"no cluster {data.cluster!r}")
            return found, f"cluster {found.name} was named by the caller"
        return await self._place(data, recipe, project, priority)

    async def _place(self, data: DeploymentCreateRequest, recipe, project, priority):
        """Asks placement, and refuses to create anything it will not answer for.

        The decision is not persisted beyond the chosen cluster and its summary.
        Every verdict it computed is reproducible by asking
        `POST /v1/placement/preview` the same question, and storing a scheduler's
        working out turns a snapshot into a record that ages badly — the numbers
        it explains are already stale by the time anyone reads them.

        A placement that requires preemption is still a placement. Which devices
        get freed inside the winning cluster is a local decision made by the
        in-cluster scheduler (`CLAUDE.md`, rule 3), so the plan travels in the
        reason rather than becoming an eviction this process performs.
        """
        svc = PlacementService(self.db)
        request = svc.request(
            recipe=recipe,
            project_uid=project.uid,
            priority=priority,
            workload=data.name,
            kind=data.type,
            replicas=data.desired_replicas,
            requires_single_domain=data.requires_single_topology_domain,
            domain_kind=data.topology_domain_kind,
        )
        decision = await svc.decide(request, project.id)
        if not decision.placed or decision.cluster is None:
            raise Conflict(
                decision.summary or "no cluster can hold this deployment",
                {"request": to_builtins(request), "verdicts": to_builtins(decision.verdicts)},
            )
        found = await self.repo.cluster_by_ref(decision.cluster)
        if found is None:
            raise Conflict("the cluster placement chose no longer exists", {"cluster": decision.cluster})
        return found, decision.summary

    async def _priority_class(self, ref: str | None, project) -> PriorityClass:
        """Resolves the priority class, falling back the way a scheduler must.

        Every workload carries one from day one, so an absent reference is not an
        absent priority: the project's default answers first, then the system
        default, and only a system with neither is a real error.
        """
        if ref is not None:
            found = await self.repo.priority_class_by_name(ref)
            if found is None:
                raise NotFound(f"no priority class {ref!r}")
            return found
        if project.default_priority_class_id is not None:
            found = await self.repo.priority_class(project.default_priority_class_id)
            if found is not None:
                return found
        fallback = await self.repo.default_priority_class()
        if fallback is None:
            raise Invalid("no priority class was given and none is marked default")
        return fallback

    async def create(self, data: DeploymentCreateRequest) -> DeploymentSchema:
        if data.type not in TYPES:
            raise Invalid(f"unknown type {data.type!r}", {"allowed": sorted(TYPES)})
        if data.gang_size < 1:
            raise Invalid("gang_size must be at least 1", {"gang_size": data.gang_size})
        check_scale(data.min_replicas, data.desired_replicas, data.max_replicas)

        recipe = await self.repo.recipe_by_ref(data.recipe)
        if recipe is None:
            raise NotFound(f"no recipe {data.recipe!r}")
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority = await self._priority_class(data.priority_class, project)
        cluster, reason = await self._cluster(data, recipe, project, priority)

        workload = Workload(
            uid=new_uid(WORKLOAD),
            kind=data.type,
            project_id=project.id,
            cluster_id=cluster.id,
            priority_class_id=priority.id,
            admission_phase="placed",
            admission_reason=reason,
            placed_at=dt.datetime.now(dt.UTC),
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
            authority=MANAGED,
            adapter_artifact_ids=await self._adapter_ids(data.adapter_artifacts),
            overrides=data.overrides,
            labels=data.labels,
            requires_single_topology_domain=data.requires_single_topology_domain,
            topology_domain_kind=data.topology_domain_kind,
        )
        try:
            await self.repo.add(workload, deployment)
            await self._record(
                deployment,
                "created",
                f"{data.name} was created on {cluster.name} from recipe {recipe.name}",
                {
                    "cluster": cluster.name,
                    "recipe": recipe.uid,
                    "desired_replicas": str(data.desired_replicas),
                    "placement_reason": reason or "",
                },
            )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"deployment {data.name!r} already exists in {cluster.name!r}") from exc
        await self._announce(cluster.uid, deployment.uid, deployment.generation)
        return await self.get(deployment.uid)

    async def set_authority(self, ref: str, data: DeploymentAuthorityRequest) -> DeploymentSchema:
        """Takes management of a deployment, or gives it back.

        The two directions are not symmetric and are not written as though they
        were. `managed` is the direction that can write to a cluster, so an adopted
        deployment has to prove the caller was looking at what is running now: the
        fingerprint they send is compared against the current sighting, not against
        `adopted_fingerprint`, which is a record of what was true when it was
        promoted. `observed` is the plane letting go and needs nothing.

        A deployment the plane created itself has no sighting and no fingerprint to
        check, and taking management of it is not a change of ownership — it is the
        ownership it was created with, made explicit (ADR 0009).
        """
        if data.authority not in AUTHORITIES:
            raise Invalid(f"unknown authority {data.authority!r}", {"allowed": sorted(AUTHORITIES)})

        row = await self._one(ref)
        deployment = row[0]
        if deployment.authority == data.authority:
            return await self._render(row)

        if data.authority == MANAGED and deployment.adopted_workload_uid:
            live = await self.repo.sighting_fingerprint(deployment.cluster_id, deployment.adopted_workload_uid)
            if live is None:
                raise Conflict(
                    "this cluster is no longer reporting the adopted workload, so there is nothing "
                    "to check the digest against",
                    {"workload": deployment.adopted_workload_uid},
                )
            if not data.fingerprint:
                raise Invalid(
                    "taking management of an adopted workload needs the fingerprint you were shown",
                    {"observed": live},
                )
            if data.fingerprint != live:
                raise Conflict(
                    "the workload changed since it was read; re-read it and try again",
                    {"observed": live, "sent": data.fingerprint},
                )
            deployment.adopted_fingerprint = live

        deployment.authority = data.authority
        deployment.generation += 1
        await self._record(
            deployment,
            "authority_changed",
            f"{deployment.name} is now {data.authority}",
            {"authority": data.authority, "generation": str(deployment.generation)},
        )
        await self.db.commit()
        await self._announce(row.cluster_uid, deployment.uid, deployment.generation)
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
            await self._record(
                deployment,
                "scaled" if "desired_replicas" in fields else "updated",
                _changed(deployment, fields, bumped),
                {
                    "fields": ", ".join(sorted(data.to_dict(exclude_unset=True))),
                    "desired_replicas": str(deployment.desired_replicas),
                    "generation": str(deployment.generation),
                },
            )
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
        # Filed before the delete, while the row it describes is still readable. `subject_id`
        # carries no foreign key precisely so that this outlives its subject — the record of a
        # deployment having been removed is worth more than the row ever was.
        await self._record(row[0], "deleted", f"{row[0].name} was deleted")
        await self.repo.delete(row[1], row[0])
        await self.db.commit()
        await self._announce(cluster_uid, uid, generation)
