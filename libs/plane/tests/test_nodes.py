import datetime as dt

import pytest

from app.core import ids
from app.db.models import Node
from app.core.exceptions import Invalid
from app.modules.nodes.routes import NodeRoutes
from app.modules.nodes.routes import parse_labels
from app.modules.nodes.schemas import NodeSchema
from app.modules.nodes.schemas import NodeDeviceRollupSchema
from app.modules.nodes.services import NodeService
from app.modules.nodes.repository import NodeRepository
from app.modules.nodes.repository import ref_ids


WRITE_METHODS = ("create", "update", "delete", "patch", "post", "add", "remove")


def compiled(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def repo() -> NodeRepository:
    return NodeRepository(db=None)


def test_service_exposes_no_write_methods():
    for name in WRITE_METHODS:
        assert not hasattr(NodeService, name)


def test_controller_only_reads():
    handlers = [h for h in vars(NodeRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    for handler in handlers:
        assert set(handler.http_methods) == {"GET"}


def test_schema_carries_no_telemetry_and_no_kubernetes_fields():
    fields = set(NodeSchema.__struct_fields__)
    assert not fields & {"utilization", "temperature_c", "power_watts", "throughput"}
    assert not fields & {"kubelet_version", "manifest", "node_ref", "provider_id"}


def test_cordoned_is_its_own_field_and_not_a_condition():
    assert "cordoned" in NodeSchema.__struct_fields__
    assert "condition" in NodeSchema.__struct_fields__


def test_list_query_filters_are_all_applied():
    sql = compiled(
        repo().query(
            limit=10,
            after_seq=42,
            cluster="iad-1",
            pool="a100",
            condition="ready",
            ready=True,
            cordoned=False,
            search="gpu",
            labels={"tier": "gold"},
        )
    )
    assert "supply_node.seq > 42" in sql
    assert "supply_cluster.name = 'iad-1'" in sql
    assert "supply_pool.name = 'a100'" in sql
    assert "supply_node.condition = 'ready'" in sql
    assert "lower(supply_node.name) LIKE lower('%gpu%')" in sql
    assert "(supply_node.labels ->> 'tier') = 'gold'" in sql
    assert "LIMIT 11" in sql
    assert "ORDER BY supply_node.seq ASC" in sql


def test_cordoned_filter_is_separate_from_condition():
    where = compiled(repo().query(limit=5, cordoned=True)).split("WHERE")[1]
    assert "supply_node.cordoned = true" in where
    assert "supply_node.condition" not in where


def test_cluster_reference_accepts_an_id_or_a_name():
    from app.db.models import Cluster

    uid = ids.new_uid(ids.CLUSTER)
    assert f"supply_cluster.uid = '{uid}'" in compiled(ref_ids(Cluster, uid))
    assert "supply_cluster.name = 'iad-1'" in compiled(ref_ids(Cluster, "iad-1"))


def test_rollup_is_one_aggregate_query_not_a_load_per_node():
    from uuid import uuid4

    sql = compiled(repo().rollup_query([uuid4(), uuid4()]))
    assert "count(*)" in sql
    assert "GROUP BY supply_device.node_id, supply_device.health" in sql


def test_class_rollup_groups_by_node_and_class():
    from uuid import uuid4

    sql = compiled(repo().class_rollup_query([uuid4()]))
    assert "supply_device.device_class" in sql
    assert "GROUP BY supply_device.node_id, supply_device.device_class" in sql


def test_allocated_query_counts_only_live_members():
    from uuid import uuid4

    sql = compiled(repo().allocated_query([uuid4()]))
    assert "bind_allocation_device" in sql
    assert "released_at IS NULL" in sql
    assert "GROUP BY supply_device.node_id" in sql


def test_summary_counts_ready_and_cordoned_nodes():
    sql = compiled(repo().summary_query())
    assert "count(supply_node.id)" in sql
    assert "supply_node.ready IS true" in sql
    assert "supply_node.cordoned IS true" in sql


def test_faulted_devices_query_is_non_healthy_devices():
    sql = compiled(repo().faulted_devices_query())
    assert "count(supply_device.id)" in sql
    assert "supply_device.health != 'healthy'" in sql


def test_schema_projection_carries_dominant_class_and_allocated():
    now = dt.datetime.now(dt.UTC)
    node = Node(
        uid=ids.new_uid(ids.NODE),
        name="node-2",
        condition="ready",
        cordoned=False,
        ready=True,
        labels={},
        taints=[],
        created_at=now,
        updated_at=None,
        cpu_cores=None,
        memory_mib=None,
        cpu_arch=None,
        os_image=None,
        kernel_version=None,
        driver_version=None,
        operator_version=None,
        joined_at=None,
        observed_at=None,
    )
    row = (node, "clu_x", "iad-1", None, None)
    schema = NodeService.schema(object.__new__(NodeService), row, {"healthy": 8}, 3, "h100")
    assert schema.devices.device_class == "h100"
    assert schema.devices.allocated == 3
    assert schema.devices.total == 8


def test_malformed_label_is_rejected():
    with pytest.raises(Invalid):
        parse_labels(["tier"])
    assert parse_labels(["tier=gold"]) == {"tier": "gold"}


def test_schema_projection_includes_the_device_rollup():
    now = dt.datetime.now(dt.UTC)
    node = Node(
        uid=ids.new_uid(ids.NODE),
        name="node-1",
        condition="ready",
        cordoned=True,
        ready=False,
        labels={},
        taints=[],
        created_at=now,
        updated_at=None,
        cpu_cores=None,
        memory_mib=None,
        cpu_arch=None,
        os_image=None,
        kernel_version=None,
        driver_version=None,
        operator_version=None,
        joined_at=None,
        observed_at=None,
    )
    row = (node, "clu_x", "iad-1", None, None)
    schema = NodeService.schema(object.__new__(NodeService), row, {"healthy": 6, "degraded": 2})
    assert schema.devices == NodeDeviceRollupSchema(total=8, by_health={"healthy": 6, "degraded": 2})
    assert schema.cordoned is True
    assert schema.ready is False
    assert schema.pool is None
