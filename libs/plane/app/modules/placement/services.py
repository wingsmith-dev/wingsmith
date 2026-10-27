"""Placement as something callable: read the snapshot, ask the engine, hand back the answer.

The service owns no transaction, and that is not an oversight. Placement returns a
decision; committing anything on the strength of one belongs to whichever module
asked: the workload service, which takes `locks.lock_placement` for the device
class before it reads the snapshot and writes the deployments in that same
transaction, so two writers cannot both spend the devices or quota one snapshot
showed free. A caller that only reads a decision, as the preview routes and the
reactor's would-it-place-more check do, needs no lock.

Responses are the decision structs from `decision.py` rather than copies of them.
They were designed as the explanation an operator reads, and mirroring them into
near-identical schemas would give a new rejection code two places to be added and
one place to be forgotten.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project
from app.db.models import Workload
from app.db.models import PriorityClass
from app.core.lookup import at_most_one
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound

from .engine import place
from .engine import expand
from .schemas import ReservationSchema
from .schemas import PlacementExpandRequest
from .schemas import PlacementPreviewRequest
from .decision import ExpansionDecision
from .decision import PlacementDecision
from .snapshot import WorkloadDemand
from .snapshot import CapacitySnapshot
from .snapshot import PlacementRequest
from .repository import PlacementRepository


PREVIEW_UID = "wkl_preview"
"""The identifier a preview of an uncreated shape is answered under.

Deliberately not a mintable uid. A preview writes nothing and reaches no cluster,
so the name only has to be stable and obviously not a row — inventing a plausible
`wkl_01m0...` would put a reference in a decision that resolves to nothing, and
the first person to paste it into a lookup would get a 404 and a wasted hour.
"""


def _single[T](found: list[T], kind: str, ref: str) -> T:
    one = at_most_one(found, kind, ref)
    if one is None:
        raise NotFound(f"no {kind} {ref!r}")
    return one


class ShapeSource(Protocol):
    """Anything that can state a device shape: a `Workload`, or a bare preview request.

    A structural type rather than a union because the two share nothing else and
    have no common base worth inventing. They agree on these six names — the
    preview request is spelled with the workload's column names precisely so that
    one builder reads either without translating — and that agreement is the whole
    contract.
    """

    device_class: str
    device_count: int
    device_memory_mib_min: int
    tensor_parallel: int
    requires_single_topology_domain: bool
    topology_domain_kind: str | None


class PlacementService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PlacementRepository(db)

    async def decide(self, request: PlacementRequest, project_id) -> PlacementDecision:
        """The one call every caller ends at: a snapshot, then the pure function."""
        snapshot = await self.repo.capacity(project_id, request.device_class)
        return place(request, snapshot)

    def request(
        self,
        workload_row: ShapeSource,
        project_uid: str,
        priority: PriorityClass,
        workload: str,
        kind: str = "service",
        replicas: int = 1,
        requires_single_domain: bool | None = None,
        domain_kind: str | None = None,
        deployment_name: str | None = None,
    ) -> PlacementRequest:
        """The workload's device shape, scaled by how many replicas are wanted.

        Multiplying is right because the plane places a workload, not a replica:
        a deployment of four tensor-parallel-eight replicas needs thirty-two
        devices in one cluster, and asking for eight would pick a cluster that
        can hold a quarter of it.

        The single-domain requirement, though, does not multiply. It is a property
        of one replica's collective — the replicas do not talk to each other over
        NVLink — so requiring all thirty-two in one NVLink domain would reject
        clusters that can run the deployment perfectly well.

        `atomic` is set because this call places one indivisible unit — one share,
        already sized by the expansion — and a share lands in one cluster whole or
        not at all. Splitting is a decision taken above this, in `expand()`.

        The topology requirement is the shape's unless the caller states its own.
        The columns describe one replica of one engine; a gang deployment is a
        shape they cannot know about, so a value given here wins and a null one
        falls through rather than clearing what was declared.

        `workload_row` is a `ShapeSource` rather than a `Workload` so that a
        preview of something not yet created reaches the same builder. Both carry
        the same six field names, which is why no translation happens here.
        """
        return PlacementRequest(
            workload=workload,
            project=project_uid,
            device_class=workload_row.device_class,
            device_count=max(workload_row.device_count, 1) * max(replicas, 1),
            kind=kind,
            priority_value=priority.value,
            preempts=priority.preempts,
            device_memory_mib_min=workload_row.device_memory_mib_min,
            tensor_parallel=max(workload_row.tensor_parallel, 1),
            requires_single_domain=(
                workload_row.requires_single_topology_domain
                if requires_single_domain is None
                else requires_single_domain
            ),
            domain_kind=workload_row.topology_domain_kind if domain_kind is None else domain_kind,
            deployment_name=deployment_name,
            atomic=True,
        )

    async def expand(self, workload: Workload, snapshot: CapacitySnapshot | None = None) -> ExpansionDecision:
        """Spread one workload's total scale over the clusters it may use.

        This is the call that turns "run gpt-20b on asia and europe" into shares.
        It resolves the workload's project and priority, reads capacity once, and
        hands both to the pure expansion; persisting the shares — creating,
        resizing and removing the per-cluster deployments — belongs to whoever
        asked, inside its own write, exactly as `decide()` does.

        A gang that fits in no single cluster raises. Its ranks are co-scheduled, so
        a split gang is broken rather than degraded and there is no partial answer
        worth returning; the engine stays total and the rejection is raised here.
        """
        demand = await self.demand(workload)
        capacity = snapshot
        if capacity is None:
            capacity = await self.repo.capacity(workload.project_id, demand.device_class, workload.id, workload.name)
        decision = expand(demand, capacity)
        if not decision.splittable and not decision.shares:
            raise Invalid(decision.summary)
        return decision

    async def demand(self, workload: Workload) -> WorkloadDemand:
        """A workload as the expansion sees it: device shape, totals, and constraints.

        The workload's own columns answer what one unit costs and the priority
        class answers what this workload may take from others; the project and the
        priority are resolved here so nothing an ORM row could lazily load reaches
        the engine.
        """
        project = await self.repo.project(workload.project_id)
        if project is None:
            raise NotFound(f"workload {workload.uid} has no project")
        priority = await self.repo.priority_class(workload.priority_class_id)
        if priority is None:
            raise Invalid(f"workload {workload.uid} has no priority class")
        pinned = (
            await self.repo.cluster_uid(workload.pinned_cluster_id) if workload.pinned_cluster_id is not None else None
        )
        return WorkloadDemand(
            workload=workload.uid,
            project=project.uid,
            name=workload.name,
            device_class=workload.device_class,
            unit_device_count=max(workload.device_count, 1),
            kind=workload.kind,
            priority_value=priority.value,
            preempts=priority.preempts,
            device_memory_mib_min=workload.device_memory_mib_min,
            tensor_parallel=max(workload.tensor_parallel, 1),
            requires_single_domain=workload.requires_single_topology_domain,
            domain_kind=workload.topology_domain_kind,
            desired_scale=workload.desired_scale,
            gang_size=max(workload.gang_size, 1),
            strategy=workload.strategy,
            cluster_floors={str(k): int(v) for k, v in (workload.cluster_floors or {}).items()},
            max_clusters=workload.max_clusters,
            pinned_cluster=pinned,
            paused=workload.paused,
            existing=await self.repo.shares(workload.id),
        )

    async def reservations(
        self, cluster: str | None = None, device_class: str | None = None
    ) -> list[ReservationSchema]:
        """What every cluster is currently owed, for the console to paint.

        The same derivation placement decides on, asked without a request to decide
        about. Reading it from one place matters: a board that computed its own idea
        of what is pending would drift from the engine the first time either changed,
        and an operator would be looking at a number the scheduler does not use.
        """
        rows = await self.repo.reservations(cluster=cluster, device_class=device_class)
        return [
            ReservationSchema(
                cluster=cluster_uid,
                cluster_name=cluster_name,
                project=unit.project,
                workload=unit.workload,
                workload_name=unit.workload_name,
                deployment=unit.deployment,
                deployment_name=unit.deployment_name,
                device_class=unit.device_class,
                devices=unit.devices,
                requires_single_domain=unit.requires_single_domain,
                domain_kind=unit.domain_kind,
            )
            for cluster_uid, cluster_name, unit in rows
        ]

    async def preview_expansion(self, data: PlacementExpandRequest) -> ExpansionDecision:
        """The split a workload would get, asked without writing a row."""
        return await self.expand(await self._workload(data.workload))

    async def preview(self, data: PlacementPreviewRequest) -> PlacementDecision:
        """Answers the question without writing a row.

        Two forms, one answer. `workload` asks about a shape that exists and hands
        the engine that workload's own uid. `shape` asks about one that does not
        exist yet — the create-form question — and hands the engine `PREVIEW_UID`,
        because nothing is being created and inventing a plausible `wkl_...` would
        put a reference in a decision that resolves to nothing.

        Project, priority and replica count are taken from the request in both
        forms rather than off any row. That is what makes this a question — what
        would this shape cost for that project, at that priority, at that scale —
        as against a report of what a workload already has.
        """
        if (data.workload is None) == (data.shape is None):
            raise Invalid("a preview names exactly one of workload and shape")
        project = await self._project(data.project)
        priority = await self._priority(data.priority_class, project)
        if data.shape is not None:
            source: ShapeSource = data.shape
            named = PREVIEW_UID
        else:
            workload = await self._workload(str(data.workload))
            source, named = workload, workload.uid
        request = self.request(
            workload_row=source,
            project_uid=project.uid,
            priority=priority,
            workload=named,
            replicas=data.replicas,
        )
        return await self.decide(request, project.id)

    async def _workload(self, ref: str) -> Workload:
        """One workload by id or name. A name is unique only per project, so two matches are refused."""
        return _single(await self.repo.workloads_by_ref(ref), "workload", ref)

    async def _project(self, ref: str) -> Project:
        """One project by id or slug. A slug is unique only per tenant, so two matches are refused."""
        return _single(await self.repo.projects_by_ref(ref), "project", ref)

    async def _priority(self, ref: str | None, project: Project) -> PriorityClass:
        """Resolves priority the way the deployment service does.

        A preview reports the priority class the create would record, so it resolves
        the class the same way: the named one, else the project's default.
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
