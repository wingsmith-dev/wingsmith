from __future__ import annotations

import datetime as dt

from uuid import UUID
from typing import NamedTuple
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Sequence

import msgspec

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import POLICY
from app.core.ids import WORKLOAD
from app.core.ids import DEPLOYMENT
from app.core.ids import WORKLOAD_REVISION
from app.core.ids import new_uid
from app.db.models import Policy
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import WorkloadRole
from app.db.models import PriorityClass
from app.db.models import PolicyAttachment
from app.db.models import WorkloadRevision
from app.db.notify import DEPLOYMENT as DEPLOYMENT_KIND
from app.db.notify import Notifier
from app.db.notify import DeclaredChange
from app.db.notify import DatabaseNotifier
from app.ingestion import statements
from app.core.schemas import ListResponse
from app.core.serving import AGGREGATED
from app.core.serving import SERVING_TOPOLOGIES
from app.core.serving import served_model
from app.core.serving import recipe_engine_args
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.placement.locks import lock_placement
from app.modules.placement.services import PlacementService
from app.modules.placement.snapshot import PolicySnapshot
from app.modules.placement.snapshot import CapacitySnapshot
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema
from app.modules.placement.repository import SUBJECT_WORKLOAD
from app.modules.deployments.repository import bump_generation

from .views import UNITS
from .views import _rollup
from .views import _status
from .views import _revision
from .views import derive_phase
from .views import _revision_view
from .schemas import TopologySchema
from .schemas import WorkloadSchema
from .schemas import WorkloadScaleSchema
from .schemas import WorkloadCreateRequest
from .schemas import WorkloadServingSchema
from .schemas import WorkloadServingUpdate
from .schemas import WorkloadUpdateRequest
from .schemas import WorkloadRevisionSchema
from .schemas import WorkloadRevisionUpdate
from .schemas import WorkloadServingRequest
from .schemas import WorkloadDrainParameters
from .schemas import WorkloadPauseParameters
from .schemas import WorkloadPlacementSchema
from .schemas import WorkloadScaleParameters
from .schemas import WorkloadDeploymentSchema
from .schemas import WorkloadReshapeParameters
from .schemas import WorkloadPlacementNodeSchema
from .schemas import WorkloadReplicaPlacementSchema
from .serving import serving_view
from .serving import apply_serving
from .serving import group_devices
from .serving import declared_roles
from .serving import serving_request
from .serving import serving_snapshot
from .serving import resolved_engine_args
from .serving import resolve_engine_runtime_image
from .repository import WorkloadRepository
from .validation import check_scale
from .validation import check_serving_request


__all__ = [
    # Re-exported for callers outside this package that predate the split into
    # `validation`, `serving` and `views`; new code imports from those modules.
    "WorkloadService",
    "check_serving_request",
    "recipe_engine_args",
    "resolved_engine_args",
    "serving_view",
]


KINDS = frozenset({"service", "task", "gang"})
SUPPORTED_KINDS = frozenset({"service"})
"""The kinds a workload can be created as today.

`task` and `gang` are on the roadmap: the plane can describe them and placement can
reason about a gang, but no operator renders either, so a workload of that kind would
be accepted and then never run. They stay in `KINDS` so a listing filter by kind
keeps answering, and are refused at creation.
"""


STRATEGIES = frozenset({"pack", "spread"})


CONSTRAINT_SUFFIX = "-placement"


PHASES = frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})


MANAGED = "managed"


def constraint_name(workload_name: str) -> str:
    """The name of the policy a workload's own allow-list becomes.

    Derived from the workload rather than random so that the policies list reads as a
    list of intentions instead of a list of identifiers, and so a second workload of the
    same name in another project collides loudly — `Policy.name` is globally unique, and
    a silent second constraint sharing one name would be worse than the conflict.
    """
    return f"{workload_name}{CONSTRAINT_SUFFIX}"


def _reshape_unavailability(before_devices: int, after_devices: int) -> dict:
    """What a flip costs while it happens, in the caller's own numbers.

    Carried in the result rather than checked, because nothing here is a refusal: whether to take
    a full weight reload now is a judgement about traffic that the plane has no input to. What it
    can do is stop the cost being a surprise, and the cost is larger than the word "reshape"
    suggests. Dynamo's worker spec hash is one value across every worker component in the graph,
    so a flip renames both roles' objects and every worker reloads its weights — twenty minutes
    apiece for a large model. The rollout then holds the old generation up while the new one
    starts, so the peak is the sum of the two footprints, and on a fleet sized for one of them the
    new generation waits on cards the old one has not let go of.
    """
    return {
        "reloads": "every worker",
        "peak_devices": before_devices + after_devices,
        "note": (
            "both roles' objects are renamed, so every worker reloads its weights, and the old "
            "generation holds its devices until the new one is up"
        ),
    }


class Announcement(NamedTuple):
    """One deployment a committed write changed, and the generation the cluster should read."""

    cluster_uid: str
    uid: str
    generation: int


@dataclass
class WorkloadChange:
    """What a workload write did before it is committed: the result to report and the clusters to wake.

    The write methods that return one flush and never commit, so a caller that records
    something about the change — an operation closing as succeeded — commits both in
    one transaction and then calls `WorkloadService.announce`.
    """

    result: dict = field(default_factory=dict)
    announcements: list[Announcement] = field(default_factory=list)
    placed: int | None = None

    def touch(self, cluster_uid: str, deployment: Deployment) -> None:
        self.announcements.append(Announcement(cluster_uid, deployment.uid, deployment.generation))

    def touch_rest(self, deployment_rows: list) -> None:
        """Announces every deployment in `deployment_rows` this change has not already announced."""
        announced = {a.uid for a in self.announcements}
        for row in deployment_rows:
            if row[0].uid not in announced:
                self.touch(row.cluster_uid, row[0])


OPERATION_PARAMETERS: dict[str, type] = {
    "workload.scale": WorkloadScaleParameters,
    "workload.pause": WorkloadPauseParameters,
    "workload.reshape": WorkloadReshapeParameters,
    "workload.drain": WorkloadDrainParameters,
}
"""The typed parameters of each plane-executed operation, decoded at the operations boundary."""


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

    async def _trace(
        self,
        workload: Workload,
        event: str,
        message: str,
        details: dict[str, str] | None = None,
        severity: str = "info",
        cluster_id: UUID | None = None,
        moment: dt.datetime | None = None,
    ) -> None:
        """Files what the plane did to the workload itself, in the caller's transaction.

        The deployment timeline can only begin once there is a deployment, which is
        after placement has already answered. Everything before that — the request
        arriving, the clusters that were considered, the reason each one was ruled
        out — happens to the workload and has nowhere else to be filed.

        `moment` is passed by a caller that is filing several events for one call, so
        that the story reads in the order it happened rather than in the order the
        writes were issued.
        """
        await self.db.execute(
            statements.workload_lifecycle(
                workload.id,
                workload.uid,
                event,
                message,
                {"name": workload.name, **(details or {})},
                moment or dt.datetime.now(dt.UTC),
                severity,
                cluster_id,
            )
        )

    async def _trace_expansion(
        self,
        workload: Workload,
        decision,
        started: dt.datetime,
        decided: dt.datetime,
    ) -> None:
        """Files placement's whole answer against the workload: what it asked, and what it got.

        Placement already reasons per cluster and returns every verdict — the counts
        it decided on and the coded reasons a cluster lost. All of that was computed
        on every expansion and then dropped on the floor, except for one summary
        string copied onto the deployments that were created. A workload that fit
        nowhere therefore created no deployment, so the one case where the reasoning
        is the entire answer is the case that discarded all of it.

        The per-cluster rejections are filed only when the expansion did not fully
        land. A workload that placed cleanly does not need seven rows saying which
        clusters did not win; one that is short does, because those rows are the next
        action.

        Each rejection also carries `cluster_id`, so it appears on that cluster's own
        timeline. "Nothing will schedule here" is a fact about the cluster, and a
        reader looking at the cluster should not have to guess which workload to open
        to find it.
        """
        placed = sum(s.replicas for s in decision.shares)
        feasible = sum(1 for v in decision.verdicts if v.feasible)
        await self._trace(
            workload,
            "placing",
            (
                f"placement considered {len(decision.verdicts)} cluster(s) for {workload.device_count} × "
                f"{workload.device_class}; {feasible} could take it"
            ),
            {
                "candidates": str(len(decision.verdicts)),
                "feasible": str(feasible),
                "device_class": workload.device_class,
                "device_count": str(workload.device_count),
                "device_memory_mib_min": str(workload.device_memory_mib_min),
                "desired_scale": str(decision.desired),
                "took_ms": str(round((decided - started).total_seconds() * 1000)),
            },
            moment=started,
        )

        landed = decided
        spread = ", ".join(f"{s.cluster_name}×{s.replicas}" for s in decision.shares if s.replicas > 0)
        outcome = {
            "placed": str(placed),
            "desired": str(decision.desired),
            "shortfall": str(decision.shortfall),
            "clusters": spread,
            "summary": decision.summary or "",
        }
        if decision.desired == 0:
            await self._trace(
                workload,
                "held",
                decision.summary or f"{workload.name} asks for nothing; every share is 0",
                outcome,
                moment=landed,
            )
            return
        if placed == 0:
            await self._trace(
                workload,
                "unplaced",
                decision.summary or f"no cluster can take {workload.name}; nothing was deployed",
                outcome,
                severity="warning",
                moment=landed,
            )
        elif decision.shortfall > 0:
            await self._trace(
                workload,
                "shortfall",
                decision.summary or f"{placed} of {decision.desired} placed; {decision.shortfall} unplaced",
                outcome,
                severity="warning",
                moment=landed,
            )
        else:
            await self._trace(
                workload,
                "placed",
                decision.summary or f"{workload.name} was placed on {spread}",
                outcome,
                moment=landed,
            )
            return

        rejected = landed + dt.timedelta(microseconds=1)
        clusters = await self.repo.clusters_by_uid([v.cluster for v in decision.verdicts if not v.feasible])
        for verdict in decision.verdicts:
            if verdict.feasible:
                continue
            cluster = clusters.get(verdict.cluster)
            await self._trace(
                workload,
                "rejected",
                f"{verdict.cluster_name}: " + "; ".join(r.message for r in verdict.reasons),
                {
                    "cluster": verdict.cluster_name,
                    "codes": ",".join(r.code for r in verdict.reasons),
                    "free_devices": str(verdict.free_devices),
                    "reserved_devices": str(verdict.reserved_devices),
                    "available_devices": str(verdict.available_devices),
                    "largest_domain_free_devices": str(verdict.largest_domain_free_devices),
                    "quota_headroom": "" if verdict.quota_headroom is None else str(verdict.quota_headroom),
                },
                cluster_id=None if cluster is None else cluster.id,
                moment=rejected,
            )

    async def _seed_revision(self, workload: Workload, pre: WorkloadServingRequest, digest: str) -> None:
        """Records what a workload was already serving, once, before its history begins.

        A workload created before `demand_workload_revision` existed has nothing in it, and the
        first edit after the upgrade would otherwise file only the state it *arrived at* —
        leaving the one configuration somebody is most likely to want back, the one they just
        replaced, unrecorded. So the pre-image every serving write already builds for its own
        validation is filed first, as `imported`.

        Does nothing for a workload that already has history, which is every workload created
        from here on. `pre` must be read before the write mutates it: `serving_request` returns
        a struct the callers go on to `setattr` their patch onto.
        """
        if await self.repo.last_revision(workload.id) is not None:
            return
        snapshot = msgspec.to_builtins(pre)
        self.db.add(
            WorkloadRevision(
                uid=new_uid(WORKLOAD_REVISION),
                workload_id=workload.id,
                number=1,
                serving_digest=digest,
                artifact_uid=snapshot.pop("artifact", ""),
                snapshot=snapshot,
                reason="imported",
            )
        )
        await self.db.flush()

    async def record_revision(
        self, workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole], reason: str
    ) -> None:
        """Appends the serving configuration the workload now holds, in the caller's transaction.

        Called after `apply_serving`, never before: what is filed has to be what was stored, the
        same rule the digest itself keeps. Declared state, and nothing reconciles against it —
        no generation moves and no cluster is told.

        Only the serving configuration is versioned. `overrides`, `labels` and the adapter list
        are on the workload and not in `WorkloadServingRequest`, so filing a revision when one
        of those moves would append a row identical to the one above it.

        A write that leaves the configuration where it was files nothing. Re-sending a patch is
        idempotent everywhere else in this service and a history that recorded the retry would
        be a history nobody trusts to mean a change.
        """
        snapshot = serving_snapshot(workload, artifact, roles)
        last = await self.repo.last_revision(workload.id)
        if last is not None and last.snapshot == snapshot:
            return
        self.db.add(
            WorkloadRevision(
                uid=new_uid(WORKLOAD_REVISION),
                workload_id=workload.id,
                number=1 if last is None else last.number + 1,
                serving_digest=workload.serving_digest,
                artifact_uid=artifact.uid,
                snapshot=snapshot,
                reason=reason,
            )
        )
        await self.db.flush()

    async def _note(self, workload: Workload, note: str | None) -> None:
        """Attaches a caller's account of a change to the revision that change produced.

        Called after the write, so the revision in force is the one the write just filed — and
        when the write filed nothing, the one it left in force. That is what makes a note
        impossible to drop: a patch carrying only a note comments on what is running, and a
        patch that arrives at the configuration already in force comments on that, rather than
        either of them being a quiet no-op.
        """
        last = await self.repo.last_revision(workload.id)
        if last is not None:
            last.note = note

    async def annotate(
        self, ref: str, revision: str, data: WorkloadRevisionUpdate, project: str | None = None
    ) -> WorkloadRevisionSchema:
        """Writes why a configuration was chosen, onto the revision that holds it.

        The only write a revision takes. Everything else about it is a record of what ran and
        editing that would be editing history; the note is an account of the record, and an
        account is usually written after the run that explains it.
        """
        workload = (await self._one(ref, project))[0]
        found = await self.repo.revision(workload.id, revision)
        if found is None:
            raise NotFound(f"no revision {revision!r} of workload {workload.uid}")
        found.note = data.note
        await self.db.commit()
        return _revision_view(found)

    async def revisions(
        self, ref: str, project: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> ListResponse[WorkloadRevisionSchema]:
        """This workload's serving history, newest first."""
        workload = (await self._one(ref, project))[0]
        size = clamp_limit(limit)
        rows = await self.repo.revisions(workload.id, size, decode_cursor(cursor))
        kept, next_cursor = page(list(rows), size, lambda r: r.number)
        items = [_revision_view(row) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def revision(self, ref: str, revision: str, project: str | None = None) -> WorkloadRevisionSchema:
        """One revision, by `wkr_` id or by the number the history lists it under."""
        workload = (await self._one(ref, project))[0]
        found = await self.repo.revision(workload.id, revision)
        if found is None:
            raise NotFound(f"no revision {revision!r} of workload {workload.uid}")
        return _revision_view(found)

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
        serving: WorkloadServingSchema,
        project_uid: str,
        priority_class: str,
        pinned_cluster_uid: str | None,
        adapter_uids: list[str],
        deployments: list[WorkloadDeploymentSchema],
        rows: list[Deployment],
        artifact_tag: str | None = None,
        served_model_name: str = "",
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
            serving_digest=workload.serving_digest,
            serving=serving,
            adapter_artifact_ids=adapter_uids,
            served_model_name=served_model_name,
            overrides=workload.overrides,
            labels=workload.labels,
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
            artifact_tag=artifact_tag,
        )

    def _deployment(
        self, row, replicas: DeploymentReplicaRollupSchema, live: dict[str, int]
    ) -> WorkloadDeploymentSchema:
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
            live_digests=live,
        )

    async def _render(self, rows: list) -> list[WorkloadSchema]:
        """One page of workloads, their expansions, and their replica counts.

        A fixed number of queries whatever the page size, and never one per row.
        """
        if not rows:
            return []
        grouped = await self.repo.deployments([r[0].id for r in rows])
        deployment_rows = [d for group in grouped.values() for d in group]
        rollup = await self.repo.replicas([d[0].id for d in deployment_rows])
        live = await self.repo.live_digests([d[0].id for d in deployment_rows])
        adapters = await self.repo.adapter_uids([i for r in rows for i in r[0].adapter_artifact_ids])
        roles = await self.repo.roles_for([r[0].id for r in rows])
        items = []
        for row in rows:
            workload = row[0]
            artifact = workload.artifact
            group = grouped.get(workload.id, [])
            items.append(
                self.schema(
                    workload=workload,
                    serving=serving_view(workload, artifact, roles.get(workload.id, [])),
                    project_uid=row.project_uid,
                    priority_class=row.priority_class,
                    pinned_cluster_uid=row.pinned_cluster_uid,
                    adapter_uids=[adapters[i] for i in workload.adapter_artifact_ids if i in adapters],
                    deployments=[
                        self._deployment(d, _rollup(rollup.get(d[0].id)), live.get(d[0].id, {})) for d in group
                    ],
                    rows=[d[0] for d in group],
                    artifact_tag=artifact.name,
                    served_model_name=served_model(workload, artifact),
                )
            )
        return items

    async def _one(self, ref: str, project: str | None = None, lock: bool = False):
        found = await self.repo.matches(ref, project, lock=lock)
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
        artifact: str | None = None,
        serving_digest: str | None = None,
        device_class: str | None = None,
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
            artifact=artifact,
            serving_digest=serving_digest,
            device_class=device_class,
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

    async def _expand(
        self,
        workload: Workload,
        exclude: str | None = None,
        extra_policies: tuple[PolicySnapshot, ...] = (),
    ):
        """Asks placement where this workload goes, and how much of it goes there.

        The shares come back whole and are never re-derived here: a share is this
        cluster's count, the workload's totals are the sum of every share, and
        computing either from the other is the bug the layering exists to prevent.

        A partial expansion is not a failure. Fewer shares than the total asked for
        means the clusters that could take some of it took some of it, and the
        shortfall is visible in the scale rollup rather than costing the caller the
        capacity that was found. A gang is the one shape with no partial answer, and
        placement raises rather than returning half of one.

        `extra_policies` exists for exactly one caller: `create`, whose workload has no
        primary key yet — the column is `server_default` — so nothing can be attached to
        it and `_policies` has nothing to find. The constraint the caller declared is
        handed to the expansion directly and persisted as a real Policy in the same
        write, so this is the only expansion in the object's life that needs it. Every
        later one reads the attachment.
        """
        await lock_placement(self.db, [workload.device_class])
        placement = PlacementService(self.db)
        snapshot = None
        if exclude is not None or extra_policies:
            demand = await placement.demand(workload)
            workload_id = getattr(workload, "id", None)
            capacity = await placement.repo.capacity(
                workload.project_id, demand.device_class, workload_id, workload.name
            )
            snapshot = CapacitySnapshot(
                clusters=tuple(c for c in capacity.clusters if exclude is None or c.uid != exclude),
                quotas=capacity.quotas,
                policies=capacity.policies + extra_policies,
            )
        return await placement.expand(workload, snapshot)

    async def create(self, data: WorkloadCreateRequest) -> WorkloadSchema:
        """Creates a workload and the deployments placement expanded it into.

        A declared `allowed_regions` or `allowed_clusters` becomes a real Policy attached
        to the workload, written in this same transaction. It is also handed to the
        expansion directly, because placement runs here and the attachment cannot exist
        yet — `Workload.id` is `server_default`, so there is no primary key to attach to
        until `repo.add` has flushed. Those are the same constraint expressed twice for
        one call only; every expansion after this one reads the attachment.

        The serving block is validated before anything is placed, and its digest is
        derived from the row rather than taken from the caller. A digest somebody else
        computed is a claim about a configuration rather than a reading of one.
        """
        requested_at = dt.datetime.now(dt.UTC)
        if data.kind not in KINDS:
            raise Invalid(f"unknown kind {data.kind!r}", {"allowed": sorted(SUPPORTED_KINDS)})
        if data.kind not in SUPPORTED_KINDS:
            raise Invalid(
                f"{data.kind} workloads are not supported yet; only service workloads run today",
                {"kind": data.kind, "supported": sorted(SUPPORTED_KINDS)},
            )
        if data.strategy not in STRATEGIES:
            raise Invalid(f"unknown strategy {data.strategy!r}", {"allowed": sorted(STRATEGIES)})
        if data.gang_size < 1:
            raise Invalid("gang_size must be at least 1", {"gang_size": data.gang_size})
        if data.max_clusters is not None and data.max_clusters < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": data.max_clusters})
        check_scale(data.min_scale, data.desired_scale, data.max_scale, data.kind, data.gang_size)

        artifact = await self.repo.artifact_by_ref(data.serving.artifact)
        if artifact is None:
            raise NotFound(f"no artifact {data.serving.artifact!r}")
        check_serving_request(data.serving, artifact)
        project = await self.repo.project_by_ref(data.project)
        if project is None:
            raise NotFound(f"no project {data.project!r}")
        priority = await self._priority_class(data.priority_class, project)

        pinned = None
        if data.cluster is not None:
            pinned = await self.repo.cluster_by_ref(data.cluster)
            if pinned is None:
                raise NotFound(f"no cluster {data.cluster!r}")

        narrowed = []
        for ref in data.allowed_clusters:
            cluster = await self.repo.cluster_by_ref(ref)
            if cluster is None:
                raise NotFound(f"no cluster {ref!r}")
            narrowed.append(cluster)
        if pinned is not None and (data.allowed_regions or narrowed):
            raise Invalid(
                "cluster pins placement to one and an allow-list asks placement to choose",
                {"cluster": data.cluster},
            )
        constraint = (
            PolicySnapshot(
                name=constraint_name(data.name),
                enabled=True,
                allowed_regions=tuple(data.allowed_regions),
                allowed_residencies=(),
                allowed_device_classes=(),
                allowed_clusters=tuple(c.uid for c in narrowed),
                denied_clusters=(),
            )
            if data.allowed_regions or narrowed
            else None
        )

        workload = Workload(
            uid=new_uid(WORKLOAD),
            name=data.name,
            kind=data.kind,
            project_id=project.id,
            priority_class_id=priority.id,
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
            deadline_at=data.deadline_at,
        )
        roles = declared_roles(data.serving.topology)
        apply_serving(workload, data.serving, artifact, roles)

        started = dt.datetime.now(dt.UTC)
        decision = await self._expand(workload, extra_policies=() if constraint is None else (constraint,))
        decided = dt.datetime.now(dt.UTC)
        shares = [s for s in decision.shares if s.replicas > 0]
        clusters = await self.repo.clusters_by_uid([s.cluster for s in shares])
        now = decided
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
            await self.repo.add(workload, deployments, roles)
            await self.record_revision(workload, artifact, roles, "created")
            if data.note is not None:
                await self._note(workload, data.note)
            if constraint is not None:
                self.db.add(
                    Policy(
                        uid=new_uid(POLICY),
                        name=constraint.name,
                        description=f"Placement constraint declared when {data.name} was created.",
                        allowed_regions=list(data.allowed_regions),
                        allowed_cluster_ids=[c.id for c in narrowed],
                        enabled=True,
                        attachments=[PolicyAttachment(subject_type=SUBJECT_WORKLOAD, subject_id=workload.id)],
                    )
                )
            await self._trace(
                workload,
                "requested",
                (
                    f"{data.name} was requested: {data.desired_scale} {UNITS[data.kind]} of "
                    f"{artifact.name} on {workload.device_count} × {workload.device_class}"
                ),
                {
                    "kind": data.kind,
                    "strategy": data.strategy,
                    "project": project.slug,
                    "artifact": artifact.uid,
                    "engine": workload.engine,
                    "engine_runtime_version": workload.engine_runtime_version,
                    "device_class": workload.device_class,
                    "device_count": str(workload.device_count),
                    "device_memory_mib_min": str(workload.device_memory_mib_min),
                    "tensor_parallel": str(workload.tensor_parallel),
                    "context_length": str(workload.context_length),
                    "desired_scale": str(data.desired_scale),
                    "cluster": data.cluster or "",
                    "serving_digest": workload.serving_digest,
                },
                moment=requested_at,
            )
            await self._trace_expansion(workload, decision, started, decided)
            for deployment, share in zip(deployments, shares, strict=True):
                await self._record(
                    deployment,
                    "created",
                    f"{data.name} was created on {share.cluster_name} serving {artifact.name}",
                    {
                        "cluster": share.cluster_name,
                        "artifact": artifact.uid,
                        "serving_digest": workload.serving_digest,
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

    async def _apply_serving_patch(self, workload: Workload, patch: WorkloadServingUpdate) -> None:
        """Merges a serving patch onto the stored configuration and rewrites the columns.

        The whole configuration is validated, not the fields the caller named, so a
        patch cannot reach a state a create would have refused — a runtime version
        bumped past what the shape can hold is caught here rather than by a cluster.

        `engine_runtime_image` re-derives when the stored one is the image this plane
        produced and the caller did not name a new one, so that moving
        `engine_runtime_version` moves the image. Carrying the stored image forward as a
        declared one made every workload pin itself the moment it was created: the
        version bump was accepted, stored, and silently ignored, and the workload
        reported the new version while the cluster went on pulling the old image. An
        image somebody pinned by hand does not match what the old engine and runtime
        version resolve to, so it survives a version bump untouched, and naming
        `engine_runtime_image` in the same patch always wins outright.
        """
        artifact = await self.repo.artifact_of(workload)
        if artifact is None:
            raise NotFound(f"workload {workload.uid} has no artifact")
        declared = {key: value for key, value in patch.to_dict().items() if value is not UNSET}
        was_derived = workload.engine_runtime_image == resolve_engine_runtime_image(
            workload.engine, workload.engine_runtime_version, None
        )
        stored_roles = await self.repo.roles_of(workload)
        request = serving_request(workload, artifact, stored_roles)
        await self._seed_revision(workload, request, workload.serving_digest)
        for key, value in declared.items():
            setattr(request, key, value)
        if was_derived and "engine_runtime_image" not in declared:
            request.engine_runtime_image = None
        check_serving_request(request, artifact)
        # An update is validated and written as the whole configuration it would leave behind, so
        # the roles are re-derived from the request rather than from the patch: a patch that says
        # nothing about the topology has already had the stored one read into `request` above, and
        # one that restates it replaces the block whole. Either way what is hashed is what is
        # stored, which is the same rule `apply_serving` keeps for every other column.
        roles = declared_roles(request.topology)
        roles = await self.repo.replace_roles(workload, roles)
        apply_serving(workload, request, artifact, roles)
        await self.record_revision(workload, artifact, roles, "updated")

    async def update(self, ref: str, data: WorkloadUpdateRequest, if_match: str | None = None) -> WorkloadSchema:
        """A partial update of what was asked for, globally.

        Scale is not here and cannot be: changing it re-expands the workload across
        clusters and can sit pending on quota indefinitely, which is the
        `workload.scale` operation. `strategy`, `cluster_floors` and `max_clusters`
        are declared here and take effect at the next expansion rather than
        reshuffling a running workload the moment they are written.

        A serving change bumps every deployment's generation, because it is the one
        kind of write here that changes the process a cluster is running.
        """
        row = await self._one(ref, lock=True)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        revision = _revision(workload, deployments)
        if if_match is not None and if_match != revision:
            raise Conflict("workload changed since it was read", {"revision": revision})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        note = fields.pop("note", UNSET)
        if "strategy" in fields and fields["strategy"] not in STRATEGIES:
            raise Invalid(f"unknown strategy {fields['strategy']!r}", {"allowed": sorted(STRATEGIES)})
        if fields.get("max_clusters") is not None and fields.get("max_clusters", 1) < 1:
            raise Invalid("max_clusters must be at least 1", {"max_clusters": fields["max_clusters"]})

        if "serving" in fields:
            await self._apply_serving_patch(workload, fields.pop("serving"))
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

        if note is not UNSET:
            await self._note(workload, note)

        bumped = bool({"overrides", "adapter_artifacts", "labels", "serving"} & set(data.to_dict(exclude_unset=True)))
        if bumped:
            for deployment in deployments:
                await bump_generation(self.db, deployment)

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

    async def _reconcile(self, workload: Workload, deployment_rows: list, exclude: str | None = None) -> WorkloadChange:
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

        Flushes and never commits: the caller decides what else lands in the same write.
        """
        started = dt.datetime.now(dt.UTC)
        decision = await self._expand(workload, exclude)
        now = dt.datetime.now(dt.UTC)
        await self._trace_expansion(workload, decision, started, now)
        shares = {s.cluster: s for s in decision.shares if s.replicas > 0}
        existing = {r.cluster_uid: r for r in deployment_rows}
        change = WorkloadChange()

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
                change.touch(cluster_uid, deployment)
                continue
            if deployment.desired_replicas != share.replicas:
                deployment.min_replicas = share.replicas
                deployment.max_replicas = share.replicas
                deployment.desired_replicas = share.replicas
                await bump_generation(self.db, deployment)
                await self._record(
                    deployment,
                    "scaled",
                    f"{workload.name} was scaled to {share.replicas} on {share.cluster_name}",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                change.touch(cluster_uid, deployment)

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
                change.touch(cluster_uid, deployment)

        change.placed = decision.placed
        change.result = {
            "summary": decision.summary,
            "shares": {s.cluster: s.replicas for s in decision.shares if s.replicas > 0},
            "touched": [a.uid for a in change.announcements],
        }
        return change

    async def announce(self, change: WorkloadChange) -> None:
        """Wakes each cluster a committed change touched. Call only after the commit."""
        for item in change.announcements:
            await self._announce(item.cluster_uid, item.uid, item.generation)

    async def _commit(self, change: WorkloadChange) -> dict:
        await self.db.commit()
        await self.announce(change)
        return change.result

    async def change_scale(self, ref: str, parameters: WorkloadScaleParameters) -> WorkloadChange:
        """Changes what was asked for, then re-expands it across clusters. Does not commit.

        The totals are validated against the kind before anything is placed, so a
        total the kind cannot honour is refused here rather than discovered by an
        engine that has already started moving shares.

        Absent parameters leave their bound alone. Sending only `desired` is the
        common case and must not silently widen or narrow the range around it.
        """
        row = await self._one(ref, lock=True)
        workload = row[0]
        min_scale = workload.min_scale if parameters.min is None else parameters.min
        max_scale = workload.max_scale if parameters.max is None else parameters.max
        desired = workload.desired_scale if parameters.desired is None else parameters.desired
        check_scale(min_scale, desired, max_scale, workload.kind, workload.gang_size)

        workload.min_scale = min_scale
        workload.max_scale = max_scale
        workload.desired_scale = desired
        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])
        return await self._reconcile(workload, deployment_rows)

    async def rescale(self, ref: str, parameters: WorkloadScaleParameters) -> dict:
        """`change_scale`, committed and announced."""
        return await self._commit(await self.change_scale(ref, parameters))

    async def change_paused(self, ref: str, paused: bool) -> WorkloadChange:
        """Parks a workload without forgetting what it was, or brings it back. Does not commit.

        Pausing disables every cluster's copy rather than deleting them or zeroing
        the workload's totals: the shares are what the workload should return to,
        and a pause that destroyed them would make resuming a re-placement with a
        different answer. Resuming re-expands, because the clusters that could take
        it may not be the ones that could when it was parked.

        Both halves announce every deployment they touched, and the resuming half
        has to say so itself. `_reconcile` announces a cluster only when its share
        moved, and a pause leaves the shares exactly where they were — so a workload
        that comes back to the split it left moves nothing, announces nothing, and
        the clusters never hear that the deployments are enabled again.
        """
        row = await self._one(ref, lock=True)
        workload = row[0]
        if workload.paused == paused:
            return WorkloadChange(result={"paused": paused, "unchanged": True})
        workload.paused = paused
        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])

        if paused:
            change = WorkloadChange(result={"paused": True})
            for deployment_row in deployment_rows:
                deployment = deployment_row[0]
                deployment.enabled = False
                await bump_generation(self.db, deployment)
                await self._record(
                    deployment,
                    "paused",
                    f"{workload.name} was paused",
                    {"workload_id": workload.uid, "generation": str(deployment.generation)},
                )
                change.touch(deployment_row.cluster_uid, deployment)
            await self.db.flush()
            return change

        for deployment_row in deployment_rows:
            deployment_row[0].enabled = True
            await bump_generation(self.db, deployment_row[0])
        change = await self._reconcile(workload, deployment_rows)
        change.touch_rest(deployment_rows)
        change.result["paused"] = False
        return change

    async def set_paused(self, ref: str, paused: bool) -> dict:
        """`change_paused`, committed and announced."""
        return await self._commit(await self.change_paused(ref, paused))

    async def change_topology(self, ref: str, parameters: WorkloadReshapeParameters) -> WorkloadChange:
        """Flips a workload between aggregated and disaggregated serving, and re-expands it.

        Does not commit. An operation rather than a `PATCH`: it changes the device footprint
        per unit, so it can sit pending on quota for as long as quota withholds the capacity —
        the failure mode `workload.scale` already exists to carry. A field write would report
        success the moment the plane accepted the intent.

        **The new count is named and never inherited.** A workload at `desired 3` means three
        aggregated replicas today and would mean three *groups* — seventy-two devices at 1:2 and
        TP8 — the moment the topology flips. The unit under the number changes, so the number is
        re-stated: a flip that does not say how many it wants is refused, not defaulted. The
        bounds follow the same argument and default to the count rather than to what they were,
        because carrying them forward is the same reinterpretation one field over.

        Every deployment's generation is bumped whether or not its share moved. `_reconcile`
        announces a cluster only when the count changed, and a flip at the same count changes no
        count at all — while changing which processes exist. The clusters would go on rendering
        the old topology against a digest that no longer describes it.
        """
        row = await self._one(ref, lock=True)
        workload = row[0]
        target = parameters.topology
        current = workload.serving_topology or AGGREGATED
        if target not in SERVING_TOPOLOGIES:
            raise Invalid(
                f"topology must be one of {sorted(SERVING_TOPOLOGIES)}",
                {"topology": target},
            )
        if target == current:
            raise Invalid(
                f"{workload.name} already serves {current}: a reshape changes which processes exist",
                {"workload": workload.uid, "serving_topology": current},
            )
        desired = parameters.desired
        if desired is None:
            raise Invalid(
                "workload.reshape needs an explicit desired count: the unit under it changes with "
                "the topology, so inheriting the old number would reinterpret it silently",
                {"parameters": ["desired"], "desired": desired},
            )
        min_scale = desired if parameters.min is None else parameters.min
        max_scale = desired if parameters.max is None else parameters.max

        artifact = await self.repo.artifact_of(workload)
        if artifact is None:
            raise NotFound(f"workload {workload.uid} has no artifact")
        stored_roles = await self.repo.roles_of(workload)
        request = serving_request(workload, artifact, stored_roles)
        await self._seed_revision(workload, request, workload.serving_digest)
        request.topology = TopologySchema(serving_topology=target, roles=list(parameters.roles))
        check_serving_request(request, artifact)
        check_scale(min_scale, desired, max_scale, workload.kind, workload.gang_size)

        before_unit = group_devices(workload.device_count, stored_roles)
        before_total = before_unit * workload.desired_scale
        roles = await self.repo.replace_roles(workload, declared_roles(request.topology))
        apply_serving(workload, request, artifact, roles)
        await self.record_revision(workload, artifact, roles, "reshaped")
        if parameters.note is not None:
            await self._note(workload, parameters.note)
        after_unit = group_devices(workload.device_count, roles)
        workload.min_scale = min_scale
        workload.max_scale = max_scale
        workload.desired_scale = desired

        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])
        for deployment_row in deployment_rows:
            deployment = deployment_row[0]
            await bump_generation(self.db, deployment)
            await self._record(
                deployment,
                "reshaped",
                f"{workload.name} was reshaped from {current} to {target}",
                {
                    "workload_id": workload.uid,
                    "generation": str(deployment.generation),
                    "serving_topology": target,
                },
            )
        change = await self._reconcile(workload, deployment_rows)
        change.touch_rest(deployment_rows)
        change.result["topology"] = {"from": current, "to": target}
        change.result["roles"] = {role.role: role.weight for role in roles}
        change.result["devices_per_unit"] = {"before": before_unit, "after": after_unit}
        change.result["scale"] = {"min": min_scale, "desired": desired, "max": max_scale}
        change.result["unavailability"] = _reshape_unavailability(before_total, after_unit * desired)
        return change

    async def reshape(self, ref: str, parameters: WorkloadReshapeParameters) -> dict:
        """`change_topology`, committed and announced."""
        return await self._commit(await self.change_topology(ref, parameters))

    async def change_drain(self, ref: str, cluster: str | None) -> WorkloadChange:
        """Takes a workload off one cluster and re-expands what is left elsewhere. Does not commit.

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
        row = await self._one(ref, lock=True)
        workload = row[0]
        found = await self.repo.cluster_by_ref(cluster)
        if found is None:
            raise NotFound(f"no cluster {cluster!r}")

        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])
        draining = [r for r in deployment_rows if r[0].cluster_id == found.id]
        if not draining:
            raise Invalid(
                f"{workload.name} is not on cluster {cluster!r}",
                {"workload": workload.uid, "cluster": cluster},
            )

        drained = WorkloadChange()
        for deployment_row in draining:
            deployment = deployment_row[0]
            await self._record(
                deployment,
                "drained",
                f"{workload.name} was drained from {cluster}",
                {"workload_id": workload.uid},
            )
            await self.db.delete(deployment)
            drained.touch(deployment_row.cluster_uid, deployment)
        await self.db.flush()

        remaining = [r for r in deployment_rows if r[0].cluster_id != found.id]
        change = await self._reconcile(workload, remaining, exclude=found.uid)
        change.announcements = drained.announcements + change.announcements
        change.result["drained"] = cluster
        return change

    async def drain(self, ref: str, cluster: str | None) -> dict:
        """`change_drain`, committed and announced."""
        return await self._commit(await self.change_drain(ref, cluster))

    async def change(self, operation_type: str, ref: str, parameters: object) -> WorkloadChange:
        """Applies one plane-executed operation with its decoded parameters. Does not commit."""
        if isinstance(parameters, WorkloadScaleParameters):
            return await self.change_scale(ref, parameters)
        if isinstance(parameters, WorkloadPauseParameters):
            return await self.change_paused(ref, parameters.paused)
        if isinstance(parameters, WorkloadReshapeParameters):
            return await self.change_topology(ref, parameters)
        if isinstance(parameters, WorkloadDrainParameters):
            return await self.change_drain(ref, parameters.cluster)
        raise Invalid(f"{operation_type} is not a workload operation", {"type": operation_type})

    async def delete(self, ref: str) -> None:
        """Deletes the workload and every cluster's copy of it.

        Replicas cascade too, and that is correct: they are a cluster's report about
        a thing that no longer exists. Ingestion writing them again before the
        cluster has caught up is a reconciliation lag, not a resurrection.
        """
        row = await self._one(ref, lock=True)
        workload = row[0]
        deployment_rows = (await self.repo.deployments([workload.id], lock=True)).get(workload.id, [])
        deployments = [d[0] for d in deployment_rows]
        announcements = [(r.cluster_uid, r[0].uid, r[0].generation) for r in deployment_rows]
        for deployment in deployments:
            await self._record(
                deployment,
                "deleted",
                f"{workload.name} was deleted",
                {"workload_id": workload.uid},
            )
        await self._trace(
            workload,
            "deleted",
            f"{workload.name} was deleted from {len(deployments)} cluster(s)",
            {"deployments": str(len(deployments))},
        )
        await self.repo.delete(workload, deployments)
        await self.db.commit()
        for cluster_uid, uid, generation in announcements:
            await self._announce(cluster_uid, uid, generation)
