from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device
from app.db.models import TopologyDomain
from app.core.schemas import ListResponse
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import DeviceSchema
from .schemas import DeviceTopologySchema
from .repository import DeviceRepository


class DeviceService:
    """Read-only, by construction, not by refusal.

    Devices are observed: ingestion is their only writer, so no create, update
    or delete method exists on this class. Reset and drain are Operations with
    their own lifecycle, not writes to this resource.

    Topology reaches the client two ways and both are intended: embedded on the
    device below as a display summary, and standalone at `/v1/topology-domains`
    for browsing and nesting. The embedded form is never the authority.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = DeviceRepository(db)

    def schema(self, row: Row) -> DeviceSchema:
        device: Device = row[0]
        domain: TopologyDomain | None = row[5]
        return DeviceSchema(
            id=device.uid,
            cluster=row[1],
            cluster_name=row[2],
            node=row[3],
            node_name=row[4],
            device_uuid=device.device_uuid,
            device_index=device.device_index,
            device_class=device.device_class,
            vendor=device.vendor,
            model_name=device.model_name,
            pci_address=device.pci_address,
            memory_mib=device.memory_mib,
            compute_capability=device.compute_capability,
            health=device.health,
            health_reason=device.health_reason,
            ecc_enabled=device.ecc_enabled,
            ecc_volatile_errors=device.ecc_volatile_errors,
            ecc_aggregate_errors=device.ecc_aggregate_errors,
            ecc_pending_retirement=device.ecc_pending_retirement,
            throttled=device.throttled,
            throttle_reasons=list(device.throttle_reasons),
            mig_enabled=device.mig_enabled,
            mig_profile=device.mig_profile,
            mig_parent_device_uuid=device.mig_parent_device_uuid,
            allocatable=device.allocatable,
            topology=(
                DeviceTopologySchema(id=domain.uid, kind=domain.kind, identifier=domain.identifier)
                if domain is not None
                else None
            ),
            observed_at=device.observed_at,
            created_at=device.created_at,
            updated_at=device.updated_at,
        )

    async def get(self, ref: str) -> DeviceSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no device {ref!r}")
        return self.schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        node: str | None = None,
        pool: str | None = None,
        device_class: str | None = None,
        health: str | None = None,
        allocatable: bool | None = None,
        topology_domain: str | None = None,
    ) -> ListResponse[DeviceSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            node=node,
            pool=pool,
            device_class=device_class,
            health=health,
            allocatable=allocatable,
            topology_domain=topology_domain,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)
