import pytest

from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.modules.clusters.routes import ClusterRoutes
from app.modules.clusters.schemas import ClusterRollupSchema
from app.modules.clusters.schemas import ClusterHeadroomSchema
from app.modules.clusters.services import _rollup
from app.modules.clusters.services import _hub_url
from app.modules.clusters.services import _setup_values
from app.modules.clusters.services import _registry_host
from app.modules.clusters.repository import ClusterRepository


@pytest.fixture
def repo():
    return ClusterRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_rollup_endpoints_are_exposed():
    handlers = {h.handler_name for h in ClusterRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert {"list_rollups", "get_rollup", "get_headroom"} <= handlers


def test_setup_endpoints_are_exposed():
    handlers = {h.handler_name for h in ClusterRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert {"cluster_setup", "issue_enrolment_token"} <= handlers


def test_hub_url_derives_from_request_host_when_unset():
    assert _hub_url("host:8080") == "wss://host:8080/v1/hub/sync"


def test_setup_values_carry_identity_and_a_pinned_image():
    out = _setup_values("cid", "wss://h/v1/hub/sync", "ghcr.io/x/operator:sha7")
    assert "id: cid" in out
    assert "hubUrl: wss://h/v1/hub/sync" in out
    assert "repository: ghcr.io/x/operator" in out
    assert "tag: sha7" in out


def test_setup_values_omit_image_when_unset():
    out = _setup_values("cid", "wss://h/v1/hub/sync", "")
    assert "operator:" not in out


def test_setup_values_fold_in_the_enrolment_token_only_when_given():
    without = _setup_values("cid", "wss://h/v1/hub/sync", "")
    assert "enrolmentToken:" not in without
    with_token = _setup_values("cid", "wss://h/v1/hub/sync", "", "hgro1.secret")
    assert "enrolmentToken:" in with_token
    assert "value: hgro1.secret" in with_token


def test_setup_values_carry_the_editable_infra_skeleton():
    out = _setup_values("cid", "wss://h/v1/hub/sync", "")
    assert "pools:" in out
    assert "labelKey:" in out
    assert "modelCache:" in out
    assert "storageClass:" in out
    assert "hfToken:" in out
    assert "serving:" in out
    assert "Edit below" in out


def test_registry_host_from_oci_chart_ref():
    assert _registry_host("oci://ghcr.io/hangar-dev/charts/cluster-setup") == "ghcr.io"


def test_node_rollup_counts_ready_in_the_database(repo):
    text = sql(repo.node_rollup_query([1, 2]))
    assert text.startswith("SELECT supply_node.cluster_id, count(supply_node.id)")
    assert "supply_node.ready IS true" in text
    assert "GROUP BY supply_node.cluster_id" in text


def test_device_rollup_counts_allocatable_in_the_database(repo):
    text = sql(repo.device_rollup_query([1]))
    assert "count(supply_device.id)" in text
    assert "supply_device.allocatable IS true" in text
    assert "GROUP BY supply_device.cluster_id" in text


def test_pool_and_deployment_counts_group_by_cluster(repo):
    pools = sql(repo.pool_rollup_query([1]))
    deployments = sql(repo.deployment_rollup_query([1]))
    assert "count(supply_pool.id)" in pools
    assert "GROUP BY supply_pool.cluster_id" in pools
    assert "count(demand_deployment.id)" in deployments
    assert "GROUP BY demand_deployment.cluster_id" in deployments


def test_whole_nodes_free_reduces_per_node_in_the_database(repo):
    text = sql(repo.whole_nodes_free_query([1]))
    assert "GROUP BY supply_device.cluster_id, supply_device.node_id" in text
    assert ".total = " in text
    assert ".free" in text


def test_largest_block_excludes_domainless_devices(repo):
    text = sql(repo.largest_block_query([1]))
    assert "supply_device.topology_domain_id IS NOT NULL" in text
    assert "supply_device.allocatable IS true" in text
    assert "max(" in text


def test_rollup_derives_allocated_as_total_minus_allocatable():
    uid = new_uid(CLUSTER)
    schema = _rollup(uid, {"devices_total": 8, "devices_allocatable": 3})
    assert isinstance(schema, ClusterRollupSchema)
    assert schema.devices_allocated == 5
    assert schema.headroom.free_devices == 3


def test_missing_row_is_all_zero():
    schema = _rollup(new_uid(CLUSTER), None)
    assert schema.nodes_total == 0
    assert schema.devices_allocated == 0
    assert schema.headroom == ClusterHeadroomSchema(
        free_devices=0, whole_nodes_free=0, largest_contiguous_block=0
    )


def test_free_block_is_floored_at_one_when_devices_are_free_but_domainless():
    schema = _rollup(new_uid(CLUSTER), {"devices_total": 4, "devices_allocatable": 2, "largest_block": 0})
    assert schema.headroom.largest_contiguous_block == 1


def test_domain_block_is_used_when_present():
    schema = _rollup(new_uid(CLUSTER), {"devices_total": 8, "devices_allocatable": 8, "largest_block": 4})
    assert schema.headroom.largest_contiguous_block == 4
