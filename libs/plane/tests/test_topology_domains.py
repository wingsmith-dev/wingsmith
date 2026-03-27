import datetime as dt

from app.core import ids
from app.db.models import TopologyDomain
from app.modules.topology_domains.routes import TopologyDomainRoutes
from app.modules.topology_domains.schemas import TopologyDomainSchema
from app.modules.topology_domains.services import TopologyDomainService
from app.modules.topology_domains.repository import TopologyDomainRepository


WRITE_METHODS = ("create", "update", "delete", "patch", "post", "add", "remove")


def compiled(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def repo() -> TopologyDomainRepository:
    return TopologyDomainRepository(db=None)


def test_service_exposes_no_write_methods():
    for name in WRITE_METHODS:
        assert not hasattr(TopologyDomainService, name)


def test_controller_only_reads_and_is_hyphenated():
    assert TopologyDomainRoutes.path == "/topology-domains"
    handlers = [h for h in vars(TopologyDomainRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    for handler in handlers:
        assert set(handler.http_methods) == {"GET"}


def test_schema_exposes_nesting_and_device_count_and_no_telemetry():
    fields = set(TopologyDomainSchema.__struct_fields__)
    assert {"parent", "device_count", "kind", "identifier", "spans_nodes"} <= fields
    assert not fields & {"utilization", "throughput", "bandwidth_used_gbps", "temperature_c"}


def test_list_query_filters_are_all_applied():
    sql = compiled(repo().query(limit=10, after_seq=3, cluster="iad-1", node="node-1", kind="nvlink", spans_nodes=True))
    assert "supply_topology_domain.seq > 3" in sql
    assert "supply_cluster.name = 'iad-1'" in sql
    assert "supply_node.name = 'node-1'" in sql
    assert "supply_topology_domain_node.domain_id" in sql
    assert "supply_topology_domain.kind = 'nvlink'" in sql
    assert "supply_topology_domain.spans_nodes = true" in sql
    assert "LIMIT 11" in sql


def test_membership_is_aggregated_so_a_spanning_domain_is_still_one_row():
    """Four member nodes must not return four copies of the domain: the page counts rows."""
    sql = compiled(repo().query(limit=10))
    assert "array_agg" in sql
    assert "JOIN supply_topology_domain_node" not in sql


def test_parent_is_resolved_by_a_self_join_not_a_lazy_load():
    sql = compiled(repo().query(limit=10))
    assert sql.count("supply_topology_domain") > 1
    assert "LEFT OUTER JOIN supply_topology_domain" in sql


def test_schema_projection():
    now = dt.datetime.now(dt.UTC)
    domain = TopologyDomain(
        uid=ids.new_uid(ids.TOPOLOGY_DOMAIN),
        kind="nvlink",
        identifier="island-0",
        link_generation="4",
        link_bandwidth_gbps=900,
        device_count=8,
        spans_nodes=False,
        attributes={},
        observed_at=None,
        created_at=now,
        updated_at=None,
    )
    row = (domain, "clu_x", "iad-1", ["nod_x", "nod_y"], ["node-1", "node-2"], "tpd_parent")
    schema = TopologyDomainService.schema(object.__new__(TopologyDomainService), row)
    assert schema.parent == "tpd_parent"
    assert schema.device_count == 8
    assert schema.nodes == ["nod_x", "nod_y"]
    assert schema.node_names == ["node-1", "node-2"]
