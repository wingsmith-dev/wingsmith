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
from app.db.models import PriorityClass
from app.db.notify import DEPLOYMENT as DEPLOYMENT_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.ingestion import statements
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.placement.services import PlacementService
from app.modules.placement.snapshot import CapacitySnapshot
from app.modules.deployments.schemas import DeploymentStatusSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema

from .schemas import WorkloadSchema
from .schemas import WorkloadScaleSchema
from .schemas import WorkloadCreateRequest
from .schemas import WorkloadUpdateRequest
from .schemas import WorkloadPlacementSchema
from .schemas import WorkloadDeploymentSchema
from .schemas import WorkloadPlacementNodeSchema
from .schemas import WorkloadReplicaPlacementSchema
from .repository import WorkloadRepository


KINDS = frozenset({"service", "task", "gang"})
STRATEGIES = frozenset({"pack", "spread"})
PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})

MANAGED = "managed"

UNITS = {"service": "replicas", "gang": "ranks", "task": "completions"}
"""What the three scale numbers count, named by kind.

The numbers are the same integers whatever the kind, and they mean three
different things; a console that renders `8` without saying whether that is eight
replicas or eight ranks is unreadable, so the unit travels with them.
"""

PHASE_ORDER = ("pending", "admitted", "placed", "running", "succeeded")
"""Admission phases from least to most advanced, for the workload rollup.

`failed` is deliberately absent: it is not a point on this line. One failed
deployment makes the workload failed regardless of how far the others got, which
is a different rule from "the least advanced wins" and is applied before it.
"""

EMPTY = DeploymentReplicaRollupSchema(
    total=0,
    ready=0,
    running=0,
    pending=0,
    failed=0,
    at_current_generation=0,
)


def check_scale(min_scale: int, desired_scale: int, max_scale: int, kind: str, gang_size: int) -> None:
    """Rejects totals that contradict each other, or a gang that pretends to a range.

    Zero is a legitimate floor — a workload scaled to nothing is parked, not
    deleted — so for a splittable kind the only rules are non-negativity and the
    ordering of the three numbers.

    A gang is different in kind and not just in degree: its ranks are co-scheduled,
    so `gang_size` of them either run together or none do. A range would say the
    plane may run part of a gang, which is not a smaller gang but a broken one.
    """
    for name, value in (("min_scale", min_scale), ("desired_scale", desired_scale), ("max_scale", max_scale)):
        if value < 0:
            raise Invalid(f"{name} cannot be negative", {name: value})
    if max_scale < min_scale:
        raise Invalid("max_scale is below min_scale", {"min_scale": min_scale, "max_scale": max_scale})
    if not min_scale <= desired_scale <= max_scale:
        raise Invalid(
            "desired_scale is outside the declared bounds",
            {"min_scale": min_scale, "desired_scale": desired_scale, "max_scale": max_scale},
        )
    if kind == "gang" and not (min_scale == desired_scale == max_scale == gang_size):
        raise Invalid(
            "a gang has no scale range: its ranks are co-scheduled, so the three totals must all equal gang_size",
            {
                "gang_size": gang_size,
                "min_scale": min_scale,
                "desired_scale": desired_scale,
                "max_scale": max_scale,
            },
        )


def derive_phase(deployments: list[Deployment]) -> str:
    """What a workload's phase is, given what became of it in each cluster.

    Derived and never stored. A workload that is running in one region and queued
    on quota in another has no single stored phase that is true, and inventing one
    would make the two indistinguishable from a workload that is halfway
    everywhere.

    A workload with no deployments at all is `pending`: placement either has not
    run yet or found nowhere to put it, and both are states it may still leave.
    """
    if not deployments:
        return "pending"
    phases = [d.admission_phase for d in deployments]
    if "failed" in phases:
        return "failed"
    ranked = [PHASE_ORDER.index(p) for p in phases if p in PHASE_ORDER]
    if not ranked:
        return "pending"
    return PHASE_ORDER[min(ranked)]


def _revision(workload: Workload, deployments: list[Deployment]) -> str:
    """The value `If-Match` is checked against, across the workload and its expansion.

    A workload is one row plus N, and a scale that only moved a share touches a
    deployment and leaves `demand_workload.updated_at` where it was. Deriving the
    revision from the workload alone would let that write go unnoticed by the next
    conditional update, so the latest stamp anywhere in the object wins.
    """
    stamps = [workload.updated_at or workload.created_at]
    stamps.extend(d.updated_at or d.created_at for d in deployments)
    return str(int(max(stamps).timestamp() * 1_000_000))


def _status(row) -> DeploymentStatusSchema | None:
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


class WorkloadService:
    """Create, read, update and delete the thing a user actually asked for.

    The service owns the commit; the repository only flushes. A workload is
    written together with the deployments placement expanded it into, in one
    transaction, because a workload whose expansion landed separately would be
    briefly indistinguishable from one nothing could place.
    """

    def __init__(self, db: AsyncSession, notifier: Notifier | None = None) -> None:
        self.db = db
        self.repo = WorkloadRepository(db)
        self.notifier: Notifier = DatabaseNotifier() if notifier is None else notifier

    async def _record(
        self,
        deployment: Deployment,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
    ) -> None:
        """Files what the plane just did, in the caller's transaction.

        Before the caller's `commit()` on purpose: the event and the write it
        describes are one fact. The timeline is keyed by deployment because that is
        where a cluster's own account of the same object is filed, and a reader
        chasing a stuck workload should not have to consult two timelines.
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

        Every caller of this is on the far side of its own `commit()`: the hub
        answers the hint by re-reading the row, so announcing first would race the
        reader against a transaction that has not landed. Missing the wakeup costs
        latency and nothing else — the next resync carries the same state.
        """
        await self.notifier.changed(
            self.db,
            DeclaredChange(cluster_id=cluster_uid, kind=DEPLOYMENT_KIND, uid=uid, generation=generation),
        )

    def schema(
        self,
        workload: Workload,
        recipe_uid: str,
        project_uid: str,
        priority_class: str,
        pinned_cluster_uid: str | None,
        adapter_uids: list[str],
        deployments: list[WorkloadDeploymentSchema],
        rows: list[Deployment],
        recipe_name: str | None = None,
        artifact_tag: str | None = None,
    ) -> WorkloadSchema:
        return WorkloadSchema(
            id=workload.uid,
            name=workload.name,
            kind=workload.kind,
            phase=derive_phase(rows),
            project_id=project_uid,
            priority_class=priority_class,
            preemptible=workload.preemptible,
            paused=workload.paused,
            gang_size=workload.gang_size,
            recipe_id=recipe_uid,
            adapter_artifact_ids=adapter_uids,
            overrides=workload.overrides,
            labels=workload.labels,
            requires_single_topology_domain=workload.requires_single_topology_domain,
            topology_domain_kind=workload.topology_domain_kind,
            strategy=workload.strategy,
            cluster_floors=workload.cluster_floors,
            max_clusters=workload.max_clusters,
            pinned_cluster_id=pinned_cluster_uid,
            scale=WorkloadScaleSchema(
                unit=UNITS.get(workload.kind, "replicas"),
                min=workload.min_scale,
                max=workload.max_scale,
                desired=workload.desired_scale,
                placed=sum(d.desired_replicas for d in deployments),
                ready=sum(d.replicas.ready for d in deployments),
            ),
            started_at=workload.started_at,
            finished_at=workload.finished_at,
            deadline_at=workload.deadline_at,
            created_at=workload.created_at,
            updated_at=workload.updated_at,
            revision=_revision(workload, rows),
            deployments=deployments,
            recipe_name=recipe_name,
            artifact_tag=artifact_tag,
        )

    def _deployment(self, row, replicas: DeploymentReplicaRollupSchema) -> WorkloadDeploymentSchema:
        deployment = row[0]
        return WorkloadDeploymentSchema(
            id=deployment.uid,
            name=deployment.name,
            cluster_id=row.cluster_uid,
            cluster_name=row.cluster_name,
            region=row.region,
            min_replicas=deployment.min_replicas,
            max_replicas=deployment.max_replicas,
            desired_replicas=deployment.desired_replicas,
            admission_phase=deployment.admission_phase,
            admission_reason=deployment.admission_reason,
            generation=deployment.generation,
            enabled=deployment.enabled,
            authority=deployment.authority,
            queued_at=deployment.queued_at,
            admitted_at=deployment.admitted_at,
            placed_at=deployment.placed_at,
            replicas=replicas,
            status=_status(row.DeploymentStatus),
        )

    async def _render(self, rows: list) -> list[WorkloadSchema]:
        """One page of workloads, their expansions, and their replica counts.

        Three queries whatever the page size, and never one per row.
        """
        if not rows:
            return []
        grouped = await self.repo.deployments([r[0].id for r in rows])
        deployment_rows = [d for group in grouped.values() for d in group]
        rollup = await self.repo.replicas([d[0].id for d in deployment_rows])
        adapters = await self.repo.adapter_uids([i for r in rows for i in r[0].adapter_artifact_ids])
        items = []
        for row in rows:
            workload = row[0]
            group = grouped.get(workload.id, [])
            items.append(
                self.schema(
                    workload=workload,
                    recipe_uid=row.recipe_uid,
                    project_uid=row.project_uid,
                    priority_class=row.priority_class,
                    pinned_cluster_uid=row.pinned_cluster_uid,
                    adapter_uids=[adapters[i] for i in workload.adapter_artifact_ids if i in adapters],
                    deployments=[self._deployment(d, _rollup(rollup.get(d[0].id))) for d in group],
                    rows=[d[0] for d in group],
                    recipe_name=row.recipe_name,
                    artifact_tag=row.artifact_tag,
                )
            )
        return items

    async def _one(self, ref: str, project: str | None = None):
        found = await self.repo.matches(ref, project)
        if not found:
            raise NotFound(f"no workload {ref!r}")
        if len(found) > 1:
            candidates = await self.repo.candidates(ref)
            raise Invalid(
                f"{ref!r} names a workload in more than one project; say which with `project`",
                {"candidates": [{"id": uid, "project": slug} for uid, slug in candidates]},
            )
        return found[0]

    async def get(self, ref: str, project: str | None = None) -> WorkloadSchema:
        rendered = await self._render([await self._one(ref, project)])
        return rendered[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        project: str | None = None,
        kind: str | None = None,
        phase: str | None = None,
        recipe: str | None = None,
        cluster: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[WorkloadSchema]:
        if kind is not None and kind not in KINDS:
            raise Invalid(f"unknown kind {kind!r}", {"allowed": sorted(KINDS)})
        if phase is not None and phase not in PHASES:
            raise Invalid(f"unknown phase {phase!r}", {"allowed": sorted(PHASES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            project=project,
            kind=kind,
            phase=phase,
            recipe=recipe,
            cluster=cluster,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = await self._render(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def placement(self, ref: str, project: str | None = None) -> WorkloadPlacementSchema:
        """Where a workload physically landed, derived from its live allocations.

        Read-only over observed state: the allocation, its devices, and the nodes
        they sit on are all written by ingestion, and nothing here writes. Only
        membership and placement are reported — device counts, node occupancy, and
        the per-replica assignment — never any serving telemetry about them.
        """
        row = await self._one(ref, project)
        workload = row[0]
        rows = await self.repo.placement(workload.id)

        nodes: dict[str, WorkloadPlacementNodeSchema] = {}
        allocations: dict[str, dict] = {}
        devices: set[str] = set()
        for member in rows:
            devices.add(member.device_uid)
            node = nodes.get(member.node_uid)
            if node is None:
                nodes[member.node_uid] = WorkloadPlacementNodeSchema(
                    node=member.node_uid,
                    cluster=member.cluster_uid,
                    device_count=1,
                )
            else:
                node.device_count += 1
            allocation = allocations.get(member.allocation_uid)
            if allocation is None:
                allocations[member.allocation_uid] = {
                    "replica": member.replica_uid,
                    "cluster": member.cluster_uid,
                    "device_count": member.device_count,
                    "nodes": [member.node_uid],
                    "devices": [member.device_uid],
                }
            else:
                if member.node_uid not in allocation["nodes"]:
                    allocation["nodes"].append(member.node_uid)
                allocation["devices"].append(member.device_uid)

        replicas = [
            WorkloadReplicaPlacementSchema(
                allocation=uid,
                replica=allocation["replica"],
                cluster=allocation["cluster"],
                nodes=allocation["nodes"],
                devices=allocation["devices"],
                device_count=allocation["device_count"],
                gang_complete=len(allocation["devices"]) >= allocation["device_count"],
            )
            for uid, allocation in allocations.items()
        ]

        return WorkloadPlacementSchema(
            workload=workload.uid,
            node_count=len(nodes),
            gpu_count=len(devices),
            nodes=list(nodes.values()),
            replicas=replicas,
        )

    async def _adapter_ids(self, refs: list[str]) -> list[UUID]:
        if not refs:
            return []
        found = await self.repo.artifacts_by_ref(refs)
        if len(found) != len(refs):
            names = {a.uid for a in found} | {a.name for a in found}
            raise NotFound("unknown adapter artifact", {"missing": sorted(set(refs) - names)})
        return [a.id for a in found]

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

    async def _expand(self, workload: Workload, exclude: str | None = None):
        """Asks placement where this workload goes, and how much of it goes there.

        The shares come back whole and are never re-derived here: a share is this
        cluster's count, the workload's totals are the sum of every share, and
        computing either from the other is the bug the layering exists to prevent.

        A partial expansion is not a failure. Fewer shares than the total asked for
        means the clusters that could take some of it took some of it, and the
        shortfall is visible in the scale rollup rather than costing the caller the
        capacity that was found. A gang is the one shape with no partial answer, and
        placement raises rather than returning half of one.
        """
        placement = PlacementService(self.db)
        snapshot = None
        if exclude is not None:
            demand = await placement.demand(workload)
            capacity = await placement.repo.capacity(workload.project_id, demand.device_class)
            snapshot = CapacitySnapshot(
                clusters=tuple(c for c in capacity.clusters if c.uid != exclude),
                quotas=capacity.quotas,
                policies=capacity.policies,
            )
        return await placement.expand(workload, snapshot)

    async def create(self, data: WorkloadCreateRequest) -> WorkloadSchema:
        if data.kind not in KINDS:
            raise Invalid(f"unknown kind {data.kind!r}", {"allowed": sorted(KINDS)})
        if data.strategy not in STRATEGIES:
            raise Invalid(f"unknown strategy {data.strategy!r}", {"allowed": sorted(STRATEGIES)})
        if data.gang_size < 1:
            raise Invalid("gang_size must be at least 1", {"gang_size": data.gang_size})
        if data.max_clusters is not None and data.max_clusters < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": data.max_clusters})
        check_scale(data.min_scale, data.desired_scale, data.max_scale, data.kind, data.gang_size)

        recipe = await self.repo.recipe_by_ref(data.recipe)
        if recipe is None:
            raise NotFound(f"no recipe {data.recipe!r}")
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority = await self._priority_class(data.priority_class, project)

        pinned = None
        if data.cluster is not None:
            pinned = await self.repo.cluster_by_ref(data.cluster)
            if pinned is None:
                raise NotFound(f"no cluster {data.cluster!r}")

        workload = Workload(
            uid=new_uid(WORKLOAD),
            name=data.name,
            kind=data.kind,
            project_id=project.id,
            priority_class_id=priority.id,
            recipe_id=recipe.id,
            min_scale=data.min_scale,
            max_scale=data.max_scale,
            desired_scale=data.desired_scale,
            strategy=data.strategy,
            cluster_floors=data.cluster_floors,
            max_clusters=data.max_clusters,
            pinned_cluster_id=None if pinned is None else pinned.id,
            gang_size=data.gang_size,
            preemptible=data.preemptible,
            paused=False,
            adapter_artifact_ids=await self._adapter_ids(data.adapter_artifacts),
            overrides=data.overrides,
            labels=data.labels,
            requires_single_topology_domain=data.requires_single_topology_domain,
            topology_domain_kind=data.topology_domain_kind,
            deadline_at=data.deadline_at,
        )

        decision = await self._expand(workload)
        shares = [s for s in decision.shares if s.replicas > 0]
        clusters = await self.repo.clusters_by_uid([s.cluster for s in shares])
        now = dt.datetime.now(dt.UTC)
        deployments = [
            Deployment(
                uid=new_uid(DEPLOYMENT),
                workload=workload,
                cluster_id=clusters[share.cluster].id,
                name=data.name,
                min_replicas=share.replicas,
                max_replicas=share.replicas,
                desired_replicas=share.replicas,
                admission_phase="placed",
                admission_reason=decision.summary or None,
                queued_at=now,
                placed_at=now,
                generation=1,
                enabled=data.enabled,
                authority=MANAGED,
            )
            for share in shares
        ]

        try:
            await self.repo.add(workload, deployments)
            for deployment, share in zip(deployments, shares, strict=True):
                await self._record(
                    deployment,
                    "created",
                    f"{data.name} was created on {share.cluster_name} from recipe {recipe.name}",
                    {
                        "cluster": share.cluster_name,
                        "recipe": recipe.uid,
                        "workload_id": workload.uid,
                        "desired_replicas": str(share.replicas),
                        "placement_reason": decision.summary or "",
                    },
                )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"workload {data.name!r} already exists in {project.slug!r}") from exc

        for row in (await self.repo.deployments([workload.id])).get(workload.id, []):
            await self._announce(row.cluster_uid, row[0].uid, row[0].generation)
        return await self.get(workload.uid)

    async def update(self, ref: str, data: WorkloadUpdateRequest, if_match: str | None = None) -> WorkloadSchema:
        """A partial update of what was asked for, globally.

        Scale is not here and cannot be: changing it re-expands the workload across
        clusters and can sit pending on quota indefinitely, which is the
        `workload.scale` operation. `strategy`, `cluster_floors` and `max_clusters`
        are declared here and take effect at the next expansion rather than
        reshuffling a running workload the moment they are written.
        """
        row = await self._one(ref)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        revision = _revision(workload, deployments)
        if if_match is not None and if_match != revision:
            raise Conflict("workload changed since it was read", {"revision": revision})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "strategy" in fields and fields["strategy"] not in STRATEGIES:
            raise Invalid(f"unknown strategy {fields['strategy']!r}", {"allowed": sorted(STRATEGIES)})
        if fields.get("max_clusters") is not None and fields.get("max_clusters", 1) < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": fields["max_clusters"]})

        if "priority_class" in fields:
            name = fields.pop("priority_class")
            found = await self.repo.priority_class_by_name(name)
            if found is None:
                raise NotFound(f"no priority class {name!r}")
            workload.priority_class_id = found.id
        if "adapter_artifacts" in fields:
            workload.adapter_artifact_ids = await self._adapter_ids(fields.pop("adapter_artifacts"))
        for key, value in fields.items():
            setattr(workload, key, value)

        bumped = bool({"overrides", "adapter_artifacts", "labels"} & set(data.to_dict(exclude_unset=True)))
        if bumped:
            for deployment in deployments:
                deployment.generation += 1

        try:
            for deployment in deployments:
                await self._record(
                    deployment,
                    "updated",
                    f"{workload.name} was updated",
                    {
                        "fields": ", ".join(sorted(data.to_dict(exclude_unset=True))),
                        "workload_id": workload.uid,
                        "generation": str(deployment.generation),
                    },
                )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("workload conflicts with an existing one") from exc

        for deployment, deployment_row in zip(deployments, deployment_rows, strict=True):
            await self._announce(deployment_row.cluster_uid, deployment.uid, deployment.generation)
        return await self.get(workload.uid)

    async def _reconcile(self, workload: Workload, deployment_rows: list, exclude: str | None = None) -> dict:
        """Re-expands a workload and moves the per-cluster deployments onto the answer.

        Three outcomes per cluster, and they are deliberately not the same write: a
        cluster that keeps a share has its counts moved and its generation bumped so
        the agent re-reads; a cluster that appears gets a deployment; a cluster whose
        share fell to zero has its deployment deleted rather than parked at zero, so
        that "which clusters is this on" stays answerable by looking at the rows.

        The generation is bumped only when the share actually moved. Re-expanding a
        workload that landed on the same split is common — anything that touches the
        workload re-derives it — and making every one of those a new generation would
        have clusters restarting replicas to arrive where they already are.
        """
        decision = await self._expand(workload, exclude)
        shares = {s.cluster: s for s in decision.shares if s.replicas > 0}
        existing = {r.cluster_uid: r for r in deployment_rows}
        now = dt.datetime.now(dt.UTC)
        touched: list[tuple[str, str, int]] = []

        for cluster_uid, row in existing.items():
            deployment = row[0]
            share = shares.pop(cluster_uid, None)
            if share is None:
                await self._record(
                    deployment,
                    "deleted",
                    f"{workload.name} left {cluster_uid}",
                    {"workload_id": workload.uid},
                )
                await self.db.delete(deployment)
                touched.append((cluster_uid, deployment.uid, deployment.generation))
                continue
            if deployment.desired_replicas != share.replicas:
                deployment.min_replicas = share.replicas
                deployment.max_replicas = share.replicas
                deployment.desired_replicas = share.replicas
                deployment.generation += 1
                await self._record(
                    deployment,
                    "scaled",
                    f"{workload.name} was scaled to {share.replicas} on {share.cluster_name}",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                touched.append((cluster_uid, deployment.uid, deployment.generation))

        if shares:
            clusters = await self.repo.clusters_by_uid(list(shares))
            for cluster_uid, share in shares.items():
                deployment = Deployment(
                    uid=new_uid(DEPLOYMENT),
                    workload=workload,
                    cluster_id=clusters[cluster_uid].id,
                    name=workload.name,
                    min_replicas=share.replicas,
                    max_replicas=share.replicas,
                    desired_replicas=share.replicas,
                    admission_phase="placed",
                    admission_reason=decision.summary or None,
                    queued_at=now,
                    admitted_at=now,
                    placed_at=now,
                    generation=1,
                    enabled=not workload.paused,
                    authority=MANAGED,
                )
                self.db.add(deployment)
                await self.db.flush()
                await self._record(
                    deployment,
                    "created",
                    f"{workload.name} was placed on {share.cluster_name}",
                    {"workload_id": workload.uid, "cluster": share.cluster_name},
                )
                touched.append((cluster_uid, deployment.uid, deployment.generation))

        return {
            "summary": decision.summary,
            "shares": {s.cluster: s.replicas for s in decision.shares if s.replicas > 0},
            "touched": [uid for _, uid, _ in touched],
            "_announce": touched,
        }

    async def _finish(self, result: dict) -> dict:
        """Commits the reconciliation, then wakes each cluster whose share moved."""
        announcements = result.pop("_announce")
        await self.db.commit()
        for cluster_uid, uid, generation in announcements:
            await self._announce(cluster_uid, uid, generation)
        return result

    async def rescale(self, ref: str, parameters: dict) -> dict:
        """Changes what was asked for, then re-expands it across clusters.

        The totals are validated against the kind before anything is placed, so a
        gang asked for a rank count its `gang_size` cannot honour is refused here
        rather than discovered by an engine that has already started moving shares.

        Absent parameters leave their bound alone. Sending only `desired` is the
        common case and must not silently widen or narrow the range around it.
        """
        row = await self._one(ref)
        workload = row[0]
        min_scale = parameters.get("min", workload.min_scale)
        max_scale = parameters.get("max", workload.max_scale)
        desired = parameters.get("desired", workload.desired_scale)
        for name, value in (("min", min_scale), ("max", max_scale), ("desired", desired)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise Invalid(f"{name} must be an integer", {name: value})
        check_scale(min_scale, desired, max_scale, workload.kind, workload.gang_size)

        workload.min_scale = min_scale
        workload.max_scale = max_scale
        workload.desired_scale = desired
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        return await self._finish(await self._reconcile(workload, deployment_rows))

    async def set_paused(self, ref: str, paused: bool) -> dict:
        """Parks a workload without forgetting what it was, or brings it back.

        Pausing disables every cluster's copy rather than deleting them or zeroing
        the workload's totals: the shares are what the workload should return to,
        and a pause that destroyed them would make resuming a re-placement with a
        different answer. Resuming re-expands, because the clusters that could take
        it may not be the ones that could when it was parked.
        """
        row = await self._one(ref)
        workload = row[0]
        if workload.paused == paused:
            return {"paused": paused, "unchanged": True}
        workload.paused = paused
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])

        if paused:
            announcements = []
            for deployment_row in deployment_rows:
                deployment = deployment_row[0]
                deployment.enabled = False
                deployment.generation += 1
                await self._record(
                    deployment,
                    "paused",
                    f"{workload.name} was paused",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                announcements.append((deployment_row.cluster_uid, deployment.uid, deployment.generation))
            await self.db.commit()
            for cluster_uid, uid, generation in announcements:
                await self._announce(cluster_uid, uid, generation)
            return {"paused": True}

        for deployment_row in deployment_rows:
            deployment_row[0].enabled = True
            deployment_row[0].generation += 1
        result = await self._reconcile(workload, deployment_rows)
        result["paused"] = False
        return await self._finish(result)

    async def drain(self, ref: str, cluster: str | None) -> dict:
        """Takes a workload off one cluster and re-expands what is left elsewhere.

        A drain is not a scale-down: the totals are untouched, so the share the
        drained cluster was carrying is offered to the others. If none of them can
        take it the workload runs short, which is visible in the scale rollup and is
        the honest outcome — quietly lowering what was asked for would hide it.

        Nothing here permanently excludes the cluster. Draining is a move, not a
        policy; keeping a workload off a cluster for good is what a Policy on it is
        for, and expressing it twice would give the two ways to disagree.
        """
        if not cluster:
            raise Invalid("workload.drain needs a cluster", {"parameters": ["cluster"]})
        row = await self._one(ref)
        workload = row[0]
        found = await self.repo.cluster_by_ref(cluster)
        if found is None:
            raise NotFound(f"no cluster {cluster!r}")

        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        draining = [r for r in deployment_rows if r[0].cluster_id == found.id]
        if not draining:
            raise Invalid(
                f"{workload.name} is not on cluster {cluster!r}",
                {"workload": workload.uid, "cluster": cluster},
            )

        announcements = []
        for deployment_row in draining:
            deployment = deployment_row[0]
            await self._record(
                deployment,
                "drained",
                f"{workload.name} was drained from {cluster}",
                {"workload_id": workload.uid},
            )
            await self.db.delete(deployment)
            announcements.append((deployment_row.cluster_uid, deployment.uid, deployment.generation))
        await self.db.flush()

        remaining = [r for r in deployment_rows if r[0].cluster_id != found.id]
        result = await self._reconcile(workload, remaining, exclude=found.uid)
        result["_announce"] = announcements + result["_announce"]
        result["drained"] = cluster
        return await self._finish(result)

    async def delete(self, ref: str) -> None:
        """Deletes the workload and every cluster's copy of it.

        Replicas cascade too, and that is correct: they are a cluster's report about
        a thing that no longer exists. Ingestion writing them again before the
        cluster has caught up is a reconciliation lag, not a resurrection.
        """
        row = await self._one(ref)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id])).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        announcements = [(r.cluster_uid, r[0].uid, r[0].generation) for r in deployment_rows]
        for deployment in deployments:
            await self._record(
                deployment,
                "deleted",
                f"{workload.name} was deleted",
                {"workload_id": workload.uid},
            )
        await self.repo.delete(workload, deployments)
        await self.db.commit()
        for cluster_uid, uid, generation in announcements:
            await self._announce(cluster_uid, uid, generation)
