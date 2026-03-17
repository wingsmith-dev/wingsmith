import datetime as dt

from app.core import ids
from app.db.models import Device
from app.db.models import TopologyDomain
from app.modules.devices.routes import DeviceRoutes
from app.modules.devices.schemas import DeviceSchema
from app.modules.devices.schemas import DeviceTopologySchema
from app.modules.devices.services import DeviceService
from app.modules.devices.repository import DeviceRepository


WRITE_METHODS = ("create", "update", "delete", "patch", "post", "add", "remove")


def compiled(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def repo() -> DeviceRepository:
    return DeviceRepository(db=None)


def test_service_exposes_no_write_methods():
    for name in WRITE_METHODS:
        assert not hasattr(DeviceService, name)


def test_controller_only_reads():
    handlers = [h for h in vars(DeviceRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    for handler in handlers:
        assert set(handler.http_methods) == {"GET"}


def test_schema_carries_state_but_no_telemetry():
    fields = set(DeviceSchema.__struct_fields__)
    assert {"health", "ecc_volatile_errors", "throttled", "mig_profile", "allocatable"} <= fields
    assert not fields & {
        "utilization",
        "gpu_utilization",
        "memory_used_mib",
        "temperature_c",
        "power_watts",
        "throughput",
    }


def test_topology_is_embedded_as_a_summary_only():
    assert DeviceSchema.__annotations__["topology"] == DeviceTopologySchema | None
    assert set(DeviceTopologySchema.__struct_fields__) == {"id", "kind", "identifier"}
    assert "parent" not in DeviceTopologySchema.__struct_fields__
    assert "device_count" not in DeviceTopologySchema.__struct_fields__


def test_hot_list_query_matches_the_composite_index_shape():
    sql = compiled(repo().query(limit=25, cluster="iad-1", device_class="h100", health="healthy"))
    assert "supply_device.cluster_id IN" in sql
    assert "supply_device.device_class = 'h100'" in sql
    assert "supply_device.health = 'healthy'" in sql


def test_list_query_joins_eagerly_and_never_lazy_loads():
    sql = compiled(repo().query(limit=25))
    assert "JOIN supply_cluster" in sql
    assert "JOIN supply_node" in sql
    assert "LEFT OUTER JOIN supply_topology_domain" in sql
    assert "supply_topology_domain.kind" in sql


def test_remaining_filters_are_applied():
    sql = compiled(
        repo().query(
            limit=5,
            after_seq=7,
            node="node-1",
            pool="a100",
            allocatable=False,
            topology_domain="nvswitch-0",
        )
    )
    assert "supply_device.seq > 7" in sql
    assert "supply_node.name = 'node-1'" in sql
    assert "supply_pool.name = 'a100'" in sql
    assert "supply_device.allocatable = false" in sql
    assert "supply_topology_domain.identifier = 'nvswitch-0'" in sql


def test_class_filter_is_exposed_as_class_on_the_wire():
    handler = vars(DeviceRoutes)["list_devices"]
    assert "device_class" in handler.fn.__annotations__


def test_schema_projection_maps_topology_and_nulls():
    now = dt.datetime.now(dt.UTC)
    device = Device(
        uid=ids.new_uid(ids.DEVICE),
        device_uuid="GPU-0",
        device_index=0,
        device_class="h100",
        vendor="nvidia",
        model_name=None,
        pci_address=None,
        memory_mib=81920,
        compute_capability=None,
        health="healthy",
        health_reason=None,
        ecc_enabled=True,
        ecc_volatile_errors=0,
        ecc_aggregate_errors=0,
        ecc_pending_retirement=False,
        throttled=False,
        throttle_reasons=[],
        mig_enabled=False,
        mig_profile=None,
        mig_parent_device_uuid=None,
        allocatable=True,
        observed_at=None,
        created_at=now,
        updated_at=None,
    )
    domain = TopologyDomain(uid=ids.new_uid(ids.TOPOLOGY_DOMAIN), kind="nvlink", identifier="island-0")
    svc = object.__new__(DeviceService)

    with_domain = DeviceService.schema(svc, (device, "clu_x", "iad-1", "nod_x", "node-1", domain))
    assert with_domain.topology == DeviceTopologySchema(id=domain.uid, kind="nvlink", identifier="island-0")

    without = DeviceService.schema(svc, (device, "clu_x", "iad-1", "nod_x", "node-1", None))
    assert without.topology is None
