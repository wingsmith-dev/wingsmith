"""The fleet summary, counted in the database.

Every number is an aggregate statement, so the cost of the read follows the
number of groups (phases, conditions, device classes), not the size of the
fleet, and nothing is paged.
"""

import datetime as dt

from sqlalchemy import Select
from sqlalchemy import ColumnElement
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Replica
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import Deployment
from app.db.models import EndpointTarget
from app.db.models import ClusterSyncState
from app.db.supply import free_device_sum

from .schemas import FleetNodesSchema
from .schemas import FleetDevicesSchema
from .schemas import FleetSummarySchema
from .schemas import FleetClustersSchema
from .schemas import FleetEndpointsSchema
from .schemas import FleetWorkloadsSchema
from .schemas import FleetDeploymentsSchema
from .schemas import FleetDeviceClassSchema


STUCK_AFTER = dt.timedelta(minutes=10)
"""How long a deployment may sit outside `running` before its shortfall counts as
unschedulable. Placement takes time; a rollout seconds old is working, not stuck."""


def _count_if(condition: ColumnElement[bool]) -> ColumnElement[int]:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _ready_by_deployment():
    return (
        select(Replica.deployment_id.label("deployment_id"), func.count(Replica.id).label("ready"))
        .where(Replica.ready.is_(True))
        .group_by(Replica.deployment_id)
        .subquery()
    )


class FleetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _grouped(self, query: Select) -> dict[str, int]:
        return {str(key): int(count) for key, count in (await self.db.execute(query)).all()}

    async def clusters(self) -> FleetClustersSchema:
        total, reachable = (
            await self.db.execute(
                select(func.count(Cluster.id), _count_if(ClusterSyncState.reachable.is_(True))).outerjoin(
                    ClusterSyncState, ClusterSyncState.cluster_id == Cluster.id
                )
            )
        ).one()
        by_phase = await self._grouped(select(Cluster.phase, func.count(Cluster.id)).group_by(Cluster.phase))
        return FleetClustersSchema(total=int(total), reachable=int(reachable), by_phase=by_phase)

    async def nodes(self) -> FleetNodesSchema:
        total, ready, cordoned = (
            await self.db.execute(
                select(func.count(Node.id), _count_if(Node.ready.is_(True)), _count_if(Node.cordoned.is_(True)))
            )
        ).one()
        by_condition = await self._grouped(select(Node.condition, func.count(Node.id)).group_by(Node.condition))
        return FleetNodesSchema(total=int(total), ready=int(ready), cordoned=int(cordoned), by_condition=by_condition)

    async def devices(self) -> FleetDevicesSchema:
        free = func.coalesce(free_device_sum(), 0)
        total, free_total = (await self.db.execute(select(func.count(Device.id), free))).one()
        by_health = await self._grouped(select(Device.health, func.count(Device.id)).group_by(Device.health))
        classes = await self.db.execute(
            select(Device.device_class, func.count(Device.id), free)
            .group_by(Device.device_class)
            .order_by(Device.device_class)
        )
        return FleetDevicesSchema(
            total=int(total),
            free=int(free_total),
            allocated=int(total) - int(free_total),
            by_health=by_health,
            by_class=[
                FleetDeviceClassSchema(
                    device_class=device_class,
                    total=int(count),
                    free=int(free_count),
                    allocated=int(count) - int(free_count),
                )
                for device_class, count, free_count in classes.all()
            ],
        )

    async def deployments(self, now: dt.datetime) -> FleetDeploymentsSchema:
        by_phase = await self._grouped(
            select(Deployment.admission_phase, func.count(Deployment.id)).group_by(Deployment.admission_phase)
        )
        replicas_ready, clusters_serving = (
            await self.db.execute(
                select(func.count(Replica.id), func.count(func.distinct(Deployment.cluster_id)))
                .select_from(Replica)
                .join(Deployment, Replica.deployment_id == Deployment.id)
                .where(Replica.ready.is_(True))
            )
        ).one()

        ready = _ready_by_deployment()
        since = func.coalesce(Deployment.placed_at, Deployment.admitted_at, Deployment.queued_at)
        shortfall = func.greatest(Deployment.desired_replicas - func.coalesce(ready.c.ready, 0), 0)
        unschedulable = (
            await self.db.execute(
                select(func.coalesce(func.sum(shortfall), 0))
                .select_from(Deployment)
                .outerjoin(ready, ready.c.deployment_id == Deployment.id)
                .where(Deployment.admission_phase != "running", since.is_(None) | (since < now - STUCK_AFTER))
            )
        ).scalar_one()

        return FleetDeploymentsSchema(
            total=sum(by_phase.values()),
            by_phase=by_phase,
            replicas_ready=int(replicas_ready),
            clusters_serving=int(clusters_serving),
            unschedulable_replicas=int(unschedulable),
            stuck_after_seconds=int(STUCK_AFTER.total_seconds()),
        )

    async def endpoints(self) -> FleetEndpointsSchema:
        ready = _ready_by_deployment()
        per_endpoint = (
            select(
                Endpoint.enabled.label("enabled"),
                func.count(Deployment.id).label("targets"),
                func.coalesce(func.sum(func.coalesce(ready.c.ready, 0)), 0).label("ready"),
                func.coalesce(func.sum(Deployment.desired_replicas), 0).label("desired"),
            )
            .select_from(Endpoint)
            .outerjoin(
                EndpointTarget,
                and_(EndpointTarget.endpoint_id == Endpoint.id, EndpointTarget.enabled.is_(True)),
            )
            .outerjoin(Deployment, Deployment.id == EndpointTarget.deployment_id)
            .outerjoin(ready, ready.c.deployment_id == Deployment.id)
            .group_by(Endpoint.id, Endpoint.enabled)
            .subquery()
        )
        enabled = per_endpoint.c.enabled.is_(True)
        targeted = enabled & (per_endpoint.c.targets > 0)
        total, disabled, idle, down, degraded, serving = (
            await self.db.execute(
                select(
                    func.count(),
                    _count_if(per_endpoint.c.enabled.is_(False)),
                    _count_if(enabled & (per_endpoint.c.targets == 0)),
                    _count_if(targeted & (per_endpoint.c.ready == 0)),
                    _count_if(targeted & (per_endpoint.c.ready > 0) & (per_endpoint.c.ready < per_endpoint.c.desired)),
                    _count_if(targeted & (per_endpoint.c.ready > 0) & (per_endpoint.c.ready >= per_endpoint.c.desired)),
                ).select_from(per_endpoint)
            )
        ).one()
        return FleetEndpointsSchema(
            total=int(total),
            disabled=int(disabled),
            serving=int(serving),
            degraded=int(degraded),
            down=int(down),
            idle=int(idle),
        )

    async def workloads(self) -> FleetWorkloadsSchema:
        total, paused = (
            await self.db.execute(select(func.count(Workload.id), _count_if(Workload.paused.is_(True))))
        ).one()
        return FleetWorkloadsSchema(total=int(total), paused=int(paused))

    async def summary(self, now: dt.datetime | None = None) -> FleetSummarySchema:
        now = now or dt.datetime.now(dt.UTC)
        return FleetSummarySchema(
            clusters=await self.clusters(),
            nodes=await self.nodes(),
            devices=await self.devices(),
            deployments=await self.deployments(now),
            endpoints=await self.endpoints(),
            workloads=await self.workloads(),
            generated_at=now,
        )
