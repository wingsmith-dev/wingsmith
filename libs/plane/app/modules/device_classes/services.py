from sqlalchemy.ext.asyncio import AsyncSession

from app.core.devices import DeviceClass
from app.core.devices import table
from app.core.schemas import ListResponse
from app.core.exceptions import NotFound

from .schemas import DeviceClassSchema
from .schemas import DeviceClassInventorySchema
from .repository import DeviceClassRepository


Counts = tuple[int, int, int, int, int, list[str] | None]
"""One row: devices, healthy, allocatable, allocated, nodes, clusters."""


def schema(name: str, entry: DeviceClass | None, counts: Counts | None) -> DeviceClassSchema:
    """One class, catalog half and fleet half, either of which may be absent."""
    return DeviceClassSchema(
        name=name,
        label=entry.label if entry is not None else name,
        architecture=entry.architecture if entry is not None else "",
        vendor=entry.vendor if entry is not None else "",
        memory_mib=entry.memory_mib if entry is not None else 0,
        form_factor=entry.form_factor if entry is not None else "",
        in_catalog=entry is not None,
        aliases={k: list(v) for k, v in entry.aliases.items()} if entry is not None else {},
        inventory=DeviceClassInventorySchema(
            devices=counts[0] if counts else 0,
            healthy=counts[1] if counts else 0,
            allocatable=counts[2] if counts else 0,
            allocated=counts[3] if counts else 0,
            nodes=counts[4] if counts else 0,
            clusters=sorted(c for c in (counts[5] or []) if c) if counts else [],
        ),
    )


def catalog(
    classes: list[DeviceClass],
    inventory: dict[str, Counts],
    architecture: str | None = None,
    present: bool | None = None,
) -> list[DeviceClassSchema]:
    """Merges the device dictionary with the fleet's inventory. Pure.

    Ordered by how much of it we own, then by name: the classes an operator
    works with every day come first without anyone having to sort, and the long
    tail of hardware we could buy sits below it in a stable order.
    """
    names = {c.name for c in classes} | set(inventory)
    by_name = {c.name: c for c in classes}
    items = [schema(name, by_name.get(name), inventory.get(name)) for name in sorted(names)]
    if architecture is not None:
        items = [i for i in items if i.architecture == architecture]
    if present is not None:
        items = [i for i in items if (i.inventory.devices > 0) is present]
    items.sort(key=lambda i: (-i.inventory.devices, i.name))
    return items


class DeviceClassService:
    """The device catalog: the contract's dictionary, joined to what we own.

    Read-only, and read-only for a different reason than the observed modules.
    A class is not discovered and not declared: it is a definition, and it is
    versioned in `libs/packages/contract/devices.json` alongside the operator
    that resolves vendor strings against it. Editing one through the API would
    put the plane and the operator on different dictionaries, which is the one
    thing the shared table exists to prevent.

    Two sets are merged here and both matter. The catalog says what we know how
    to name; the fleet says what we actually have. A class in the catalog with
    no devices is hardware we could take delivery of tomorrow; a class in the
    fleet with no catalog entry is hardware nobody has named yet, and it is the
    more urgent of the two.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = DeviceClassRepository(db)
        self.table = table()

    async def _inventory(self, cluster: str | None) -> dict[str, Counts]:
        rows = await self.repo.inventory(cluster=cluster)
        return {row[0]: tuple(row[1:]) for row in rows}

    async def list(
        self,
        cluster: str | None = None,
        architecture: str | None = None,
        present: bool | None = None,
    ) -> ListResponse[DeviceClassSchema]:
        """Every class, catalog and fleet unioned.

        Not paginated: the catalog is a bounded dictionary of hardware we can
        buy, not a collection that grows with use. `present` narrows it to what
        this fleet, or one cluster of it, actually holds.
        """
        inventory = await self._inventory(cluster)
        items = catalog(self.table.classes, inventory, architecture=architecture, present=present)
        return ListResponse(items=items, count=len(items))

    async def get(self, name: str, cluster: str | None = None) -> DeviceClassSchema:
        """One class. Known to the catalog or present in the fleet is enough."""
        entry = self.table.get(name)
        inventory = await self._inventory(cluster)
        counts = inventory.get(name)
        if entry is None and counts is None:
            raise NotFound(f"no device class {name!r}")
        return schema(name, entry, counts)
