"""The read side of placement: Postgres in, `CapacitySnapshot` out.

This file exists so `engine.py` never has to. Placement is worth having as a pure
function only if something else does the I/O, and that something has to live
somewhere — putting it in the same directory keeps the module shaped like every
other one (`CLAUDE.md`), while the purity rule stays exactly where it was: on
`engine.py`, `snapshot.py` and `decision.py`, none of which import this.

Every value handed to the engine is a plain struct built here. Nothing an ORM row
escapes into, because a lazy attribute reaching the engine would be I/O inside a
function whose whole value is that it has none.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Quota
from app.db.models import Device
from app.db.models import Policy
from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Project
from app.db.models import Workload
from app.db.models import Allocation
from app.db.models import Deployment
from app.db.models import PriorityClass
from app.db.models import TopologyDomain
from app.db.models import AllocationDevice
from app.db.models import ClusterSyncState
from app.db.models import PolicyAttachment

from .snapshot import DeviceHold
from .snapshot import QuotaSnapshot
from .snapshot import DeviceSnapshot
from .snapshot import PolicySnapshot
from .snapshot import ClusterSnapshot
from .snapshot import CapacitySnapshot
from .snapshot import TopologyDomainSnapshot


HEALTHY = "healthy"
"""The one device health that counts as usable.

Everything else — `degraded`, `unhealthy`, `unknown` — is not free, and `unknown`
belongs on that side of the line: a device we have no current report for is a
device we cannot promise.
"""

SUBJECT_PROJECT = "project"
SUBJECT_TENANT = "tenant"
SUBJECT_WORKLOAD = "workload"


class PlacementRepository:
    """Reads only. Placement decides; persisting the decision belongs to the caller."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def capacity(self, project_id: UUID, device_class: str, workload_id: UUID | None = None) -> CapacitySnapshot:
        """Everything placement is allowed to look at, for one project and device class.

        Narrowed to the device class on purpose. A control plane that snapshots
        every accelerator it owns to place one deployment reads the whole fleet
        for an answer that could only ever have concerned one slice of it.

        `workload_id` is optional because two callers legitimately have no workload to
        name: `preview`, which is asked about a recipe before anything exists, and the
        create path, which has no primary key until it flushes. Both are answered without
        workload-level policies, and create compensates by handing its own constraint
        straight to the expansion.
        """
        clusters = await self._clusters(device_class)
        return CapacitySnapshot(
            clusters=clusters,
            quotas=await self._quotas(project_id, device_class),
            policies=await self._policies(project_id, workload_id),
        )

    async def _clusters(self, device_class: str) -> tuple[ClusterSnapshot, ...]:
        rows = (
            await self.db.execute(
                select(Cluster, ClusterSyncState.reachable).outerjoin(
                    ClusterSyncState, ClusterSyncState.cluster_id == Cluster.id
                )
            )
        ).all()
        devices = await self._devices(device_class)
        domains = await self._domains()
        taken = await self._taken_names()
        return tuple(
            ClusterSnapshot(
                uid=cluster.uid,
                name=cluster.name,
                region=cluster.region,
                phase=cluster.phase,
                reachable=bool(reachable),
                zone=cluster.zone,
                residency=cluster.residency,
                devices=tuple(devices.get(cluster.id, ())),
                domains=tuple(domains.get(cluster.id, ())),
                labels=dict(cluster.labels),
                taken_names=taken.get(cluster.id, {}),
            )
            for cluster, reachable in rows
        )

    async def _devices(self, device_class: str) -> dict[UUID, list[DeviceSnapshot]]:
        """Every device of the class, with whoever holds it attached.

        The hold is a left join rather than a second query because a device's
        freedom and its holder's priority are the same question asked twice, and
        answering them from two reads lets a preemption plan be computed against
        a device that was released in between.
        """
        rows = (
            await self.db.execute(
                select(
                    Device,
                    Node.name,
                    Node.cordoned,
                    Node.ready,
                    TopologyDomain.uid,
                    Workload.uid,
                    Deployment.uid,
                    Project.uid,
                    PriorityClass.value,
                    PriorityClass.preemptible,
                )
                .join(Node, Device.node_id == Node.id)
                .outerjoin(TopologyDomain, Device.topology_domain_id == TopologyDomain.id)
                .outerjoin(
                    AllocationDevice,
                    (AllocationDevice.device_id == Device.id) & AllocationDevice.released_at.is_(None),
                )
                .outerjoin(
                    Allocation,
                    (Allocation.id == AllocationDevice.allocation_id) & Allocation.released_at.is_(None),
                )
                .outerjoin(Workload, Allocation.workload_id == Workload.id)
                .outerjoin(Deployment, Allocation.deployment_id == Deployment.id)
                .outerjoin(Project, Allocation.project_id == Project.id)
                .outerjoin(PriorityClass, Workload.priority_class_id == PriorityClass.id)
                .where(Device.device_class == device_class)
            )
        ).all()

        by_cluster: dict[UUID, list[DeviceSnapshot]] = {}
        for (
            device,
            node_name,
            cordoned,
            node_ready,
            domain_uid,
            workload_uid,
            deployment_uid,
            project_uid,
            value,
            preemptible,
        ) in rows:
            hold = None
            if workload_uid is not None and project_uid is not None:
                hold = DeviceHold(
                    workload=workload_uid,
                    deployment=deployment_uid,
                    project=project_uid,
                    priority_value=value or 0,
                    preemptible=bool(preemptible),
                )
            by_cluster.setdefault(device.cluster_id, []).append(
                DeviceSnapshot(
                    uid=device.uid,
                    node=node_name,
                    device_class=device.device_class,
                    memory_mib=device.memory_mib,
                    healthy=device.health == HEALTHY and not device.throttled,
                    allocatable=device.allocatable,
                    schedulable=bool(node_ready) and not cordoned,
                    domain=domain_uid,
                    held_by=hold,
                )
            )
        return by_cluster

    async def _taken_names(self) -> dict[UUID, dict[str, str]]:
        """Which deployment name is spoken for in which cluster, and by whom.

        A deployment name is unique per cluster, so expanding a workload onto a
        cluster where a *different* workload already holds that name cannot succeed.
        The map is carried into the snapshot so the check costs the engine a
        dictionary lookup instead of a query.
        """
        rows = (
            await self.db.execute(
                select(Deployment.cluster_id, Deployment.name, Workload.uid).join(
                    Workload, Deployment.workload_id == Workload.id
                )
            )
        ).all()
        taken: dict[UUID, dict[str, str]] = {}
        for cluster_id, name, workload_uid in rows:
            taken.setdefault(cluster_id, {})[name] = workload_uid
        return taken

    async def _domains(self) -> dict[UUID, list[TopologyDomainSnapshot]]:
        rows = (
            await self.db.execute(select(TopologyDomain, TopologyDomain.parent_id).order_by(TopologyDomain.seq))
        ).all()
        parents = {d.id: d.uid for d, _ in rows}
        by_cluster: dict[UUID, list[TopologyDomainSnapshot]] = {}
        for domain, parent_id in rows:
            by_cluster.setdefault(domain.cluster_id, []).append(
                TopologyDomainSnapshot(
                    uid=domain.uid,
                    kind=domain.kind,
                    identifier=domain.identifier,
                    parent=parents.get(parent_id) if parent_id is not None else None,
                    spans_nodes=domain.spans_nodes,
                )
            )
        return by_cluster

    async def _quotas(self, project_id: UUID, device_class: str) -> tuple[QuotaSnapshot, ...]:
        """The project's quota rows, each carrying the draw that applies to its own scope.

        The declared half is a row and the observed half is a sum over live
        allocations, and they are joined here rather than stored together because
        they have different writers — a quota is intent, an allocation is what a
        cluster reported.
        """
        rows = (
            await self.db.execute(
                select(Quota, Project.uid, Cluster.uid)
                .join(Project, Quota.project_id == Project.id)
                .outerjoin(Cluster, Quota.cluster_id == Cluster.id)
                .where(Quota.project_id == project_id, Quota.device_class == device_class)
            )
        ).all()
        if not rows:
            return ()

        drawn = await self._allocated(project_id, device_class)
        total = sum(drawn.values())
        return tuple(
            QuotaSnapshot(
                project=project_uid,
                device_class=quota.device_class,
                cluster=cluster_uid,
                guaranteed_devices=quota.guaranteed_devices,
                limit_devices=quota.limit_devices,
                allocated_devices=drawn.get(quota.cluster_id, 0) if quota.cluster_id is not None else total,
                over_quota_enabled=quota.over_quota_enabled,
            )
            for quota, project_uid, cluster_uid in rows
        )

    async def _allocated(self, project_id: UUID, device_class: str) -> dict[UUID, int]:
        """Live devices this project holds of this class, per cluster.

        Counted from `AllocationDevice` rather than `Allocation.device_count`
        because the count is what was asked for and the rows are what was given;
        a partially released allocation makes the two disagree.
        """
        rows = (
            await self.db.execute(
                select(Allocation.cluster_id, AllocationDevice.id)
                .join(AllocationDevice, AllocationDevice.allocation_id == Allocation.id)
                .where(
                    Allocation.project_id == project_id,
                    Allocation.device_class == device_class,
                    Allocation.released_at.is_(None),
                    AllocationDevice.released_at.is_(None),
                )
            )
        ).all()
        drawn: dict[UUID, int] = {}
        for cluster_id, _ in rows:
            drawn[cluster_id] = drawn.get(cluster_id, 0) + 1
        return drawn

    async def _policies(self, project_id: UUID, workload_id: UUID | None = None) -> tuple[PolicySnapshot, ...]:
        """Policies attached to the workload, its project, or the tenant above it.

        Attachment resolution happens here so the engine receives only policies
        that apply. A tenant policy reaches every project inside it; there is no
        precedence between the three levels, because a constraint that a narrower
        attachment could switch off would not be a constraint. A workload policy is
        therefore additive: it can only ever narrow what the project already allowed.
        """
        tenant_id = await self.db.scalar(select(Project.tenant_id).where(Project.id == project_id))
        subjects = [(SUBJECT_PROJECT, project_id)]
        if tenant_id is not None:
            subjects.append((SUBJECT_TENANT, tenant_id))
        if workload_id is not None:
            subjects.append((SUBJECT_WORKLOAD, workload_id))

        rows = (
            (
                await self.db.execute(
                    select(Policy)
                    .join(PolicyAttachment, PolicyAttachment.policy_id == Policy.id)
                    .where(
                        PolicyAttachment.subject_type.in_([s for s, _ in subjects]),
                        PolicyAttachment.subject_id.in_([i for _, i in subjects]),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return ()

        referenced = {i for p in rows for i in list(p.allowed_cluster_ids) + list(p.denied_cluster_ids)}
        uids = await self._cluster_uids(referenced)
        return tuple(
            PolicySnapshot(
                name=policy.name,
                enabled=policy.enabled,
                allowed_regions=tuple(policy.allowed_regions),
                allowed_residencies=tuple(policy.allowed_residencies),
                allowed_device_classes=tuple(policy.allowed_device_classes),
                allowed_clusters=tuple(uids[i] for i in policy.allowed_cluster_ids if i in uids),
                denied_clusters=tuple(uids[i] for i in policy.denied_cluster_ids if i in uids),
            )
            for policy in rows
        )

    async def _cluster_uids(self, ids: set[UUID]) -> dict[UUID, str]:
        """Policies store cluster primary keys; the engine compares public ids."""
        if not ids:
            return {}
        rows = (await self.db.execute(select(Cluster.id, Cluster.uid).where(Cluster.id.in_(ids)))).all()
        return {i: uid for i, uid in rows}

    async def workload_by_ref(self, ref: str) -> Workload | None:
        result = await self.db.execute(
            select(Workload).where((Workload.uid if is_uid(ref) else Workload.name) == ref).limit(1)
        )
        return result.scalar_one_or_none()

    async def recipe(self, recipe_id: UUID) -> Recipe | None:
        return await self.db.get(Recipe, recipe_id)

    async def project(self, project_id: UUID) -> Project | None:
        return await self.db.get(Project, project_id)

    async def cluster_uid(self, cluster_id: UUID) -> str | None:
        return await self.db.scalar(select(Cluster.uid).where(Cluster.id == cluster_id))

    async def shares(self, workload_id: UUID) -> dict[str, int]:
        """This workload's current per-cluster shares, keyed by cluster uid.

        Re-expansion is stable only if it can see what is already running, which is
        what keeps a scale-up from reshuffling a workload that is serving traffic.
        """
        rows = (
            await self.db.execute(
                select(Cluster.uid, Deployment.desired_replicas)
                .join(Cluster, Deployment.cluster_id == Cluster.id)
                .where(Deployment.workload_id == workload_id)
            )
        ).all()
        return {uid: replicas for uid, replicas in rows}

    async def recipe_by_ref(self, ref: str) -> Recipe | None:
        """Newest version wins, matching how a deployment resolves the same reference."""
        column = Recipe.uid if is_uid(ref) else Recipe.name
        result = await self.db.execute(select(Recipe).where(column == ref).order_by(Recipe.version.desc()).limit(1))
        return result.scalar_one_or_none()

    async def project_by_ref(self, ref: str) -> Project | None:
        column = Project.uid if is_uid(ref) else Project.slug
        result = await self.db.execute(select(Project).where(column == ref).limit(1))
        return result.scalar_one_or_none()

    async def priority_class_by_name(self, name: str) -> PriorityClass | None:
        result = await self.db.execute(select(PriorityClass).where(PriorityClass.name == name).limit(1))
        return result.scalar_one_or_none()

    async def priority_class(self, priority_class_id: UUID) -> PriorityClass | None:
        return await self.db.get(PriorityClass, priority_class_id)

    async def default_priority_class(self) -> PriorityClass | None:
        result = await self.db.execute(
            select(PriorityClass)
            .where(PriorityClass.is_default.is_(True))
            .order_by(PriorityClass.value.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
