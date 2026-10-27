import datetime as dt

from app.core.schemas import Struct


class DeviceTopologySchema(Struct):
    """The device's topology domain, inlined for display.

    Topology is reachable two ways and both are correct: embedded here as a
    summary, and standalone at `/v1/topology-domains` for browsing and for the
    nesting. This form is a summary and never the authority: it carries no
    parent, no membership and no counts, so anything that needs the domain
    itself must read the collection.
    """

    id: str
    kind: str
    identifier: str


class DeviceSchema(Struct):
    """One accelerator: the schedulable unit, and always observed.

    Health, ECC counters, throttle state, MIG profile and allocatability are
    state and belong here. Utilization, temperature, power and throughput are
    telemetry and go to the observability stack directly, so they never appear
    on this schema.
    """

    id: str
    cluster: str
    cluster_name: str
    node: str
    node_name: str
    device_uuid: str
    device_index: int
    device_class: str
    vendor: str
    model_name: str | None
    pci_address: str | None
    memory_mib: int
    compute_capability: str | None
    health: str
    health_reason: str | None
    ecc_enabled: bool
    ecc_volatile_errors: int
    ecc_aggregate_errors: int
    ecc_pending_retirement: bool
    throttled: bool
    throttle_reasons: list[str]
    mig_enabled: bool
    mig_profile: str | None
    mig_parent_device_uuid: str | None
    allocatable: bool
    topology: DeviceTopologySchema | None
    observed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
