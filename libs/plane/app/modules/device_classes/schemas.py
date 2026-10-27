from app.core.schemas import Struct


class DeviceClassInventorySchema(Struct):
    """What the fleet actually holds of one class, right now.

    Observed, and counted from `supply_device`, not stored: a catalog
    entry is a dictionary definition and has no state of its own, so the numbers
    beside it are a read of the fleet and never a field on the class.

    `allocatable` is free, claimable and unclaimed, and `allocated` is held by
    an unreleased allocation. They are counted apart because neither is the
    complement of the other: a device that is not claimable and not held is
    neither, and a device can be held while unhealthy.
    """

    devices: int
    healthy: int
    allocatable: int
    allocated: int
    nodes: int
    clusters: list[str]


class DeviceClassSchema(Struct):
    """One GPU model, as the plane names it.

    `name` is the identifier: the string that appears on a pool, a workload's
    shape, a quota and a device, and the only one that travels on the wire.
    `label` is the one to put in front of a person.

    A class can exist without a catalog entry: hardware the contract table does
    not list still resolves, to the slug of its vendor string, and still gets
    scheduled onto. Those come back with `in_catalog` false and no vendor
    metadata, which is the signal to grow `libs/packages/contract/devices.json`.
    """

    name: str
    label: str
    architecture: str
    vendor: str
    memory_mib: int
    form_factor: str
    in_catalog: bool
    aliases: dict[str, list[str]]
    inventory: DeviceClassInventorySchema
