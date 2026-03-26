"""Placement as something callable: read the snapshot, ask the engine, hand back the answer.

The service owns no transaction, and that is not an oversight. Placement returns a
decision; committing anything on the strength of one belongs to whichever module
asked — today the deployment service, which is already inside its own write.

Responses are the decision structs from `decision.py` rather than copies of them.
They were designed as the explanation an operator reads, and mirroring them into
near-identical schemas would give a new rejection code two places to be added and
one place to be forgotten.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Recipe
from app.db.models import Project
from app.db.models import PriorityClass
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound

from .engine import place
from .schemas import PlacementPreviewRequest
from .decision import PlacementDecision
from .snapshot import PlacementRequest
from .repository import PlacementRepository


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
        recipe: Recipe,
        project_uid: str,
        priority: PriorityClass,
        workload: str,
        kind: str = "service",
        replicas: int = 1,
    ) -> PlacementRequest:
        """The recipe's device shape, scaled by how many replicas are wanted.

        Multiplying is right because the plane places a workload, not a replica:
        a deployment of four tensor-parallel-eight replicas needs thirty-two
        devices in one cluster, and asking for eight would pick a cluster that
        can hold a quarter of it.

        The single-domain requirement, though, does not multiply. It is a property
        of one replica's collective — the replicas do not talk to each other over
        NVLink — so requiring all thirty-two in one NVLink domain would reject
        clusters that can run the deployment perfectly well.
        """
        return PlacementRequest(
            workload=workload,
            project=project_uid,
            device_class=recipe.device_class,
            device_count=max(recipe.device_count, 1) * max(replicas, 1),
            kind=kind,
            priority_value=priority.value,
            preempts=priority.preempts,
            device_memory_mib_min=recipe.device_memory_mib_min,
            requires_single_domain=recipe.requires_single_topology_domain,
            domain_kind=recipe.topology_domain_kind,
        )

    async def preview(self, data: PlacementPreviewRequest) -> PlacementDecision:
        """Answers the question without writing a row.

        `workload` is the recipe's own id rather than an invented one: nothing is
        being created, so naming a workload that will never exist would put a
        dangling reference in the operator's hands.
        """
        recipe = await self._recipe(data.recipe)
        project = await self._project(data.project)
        priority = await self._priority(data.priority_class, project)
        request = self.request(
            recipe=recipe,
            project_uid=project.uid,
            priority=priority,
            workload=recipe.uid,
            replicas=data.replicas,
        )
        return await self.decide(request, project.id)

    async def _recipe(self, ref: str) -> Recipe:
        found = await self.repo.recipe_by_ref(ref)
        if found is None:
            raise NotFound(f"no recipe {ref!r}")
        return found

    async def _project(self, ref: str) -> Project:
        found = await self.repo.project_by_ref(ref)
        if found is None:
            raise NotFound(f"no project {ref!r}")
        return found

    async def _priority(self, ref: str | None, project: Project) -> PriorityClass:
        """Resolves priority the way the deployment service does, and for the same reason.

        A preview run at a different priority from the create it is previewing
        would answer a question nobody asked: priority decides what this workload
        may preempt, which is half of whether it fits.
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
