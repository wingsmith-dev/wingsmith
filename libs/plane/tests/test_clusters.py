import pytest

from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.modules.clusters.routes import ClusterRoutes
from app.modules.clusters.schemas import ClusterRollupSchema
from app.modules.clusters.schemas import ClusterHeadroomSchema
from app.modules.clusters.services import SetupValues
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


def values(**overrides) -> SetupValues:
    fields = {"cluster_name": "cid", "hub_url": "wss://h/v1/hub/sync", "region": "us-east-1"}
    return SetupValues(**{**fields, **overrides})


def test_setup_values_carry_identity_and_a_pinned_image():
    out = _setup_values(values(operator_image="ghcr.io/x/operator:sha7"))
    assert "id: cid" in out
    assert "hubUrl: wss://h/v1/hub/sync" in out
    assert "repository: ghcr.io/x/operator" in out
    assert "tag: sha7" in out


def test_setup_values_omit_image_when_unset():
    out = _setup_values(values())
    assert "operator:" not in out


def test_setup_values_fold_in_the_enrolment_token_only_when_given():
    without = _setup_values(values())
    assert "enrolmentToken:" not in without
    with_token = _setup_values(values(enrolment_token="hgro1.secret"))
    assert "enrolmentToken:" in with_token
    assert "value: hgro1.secret" in with_token


def test_setup_values_carry_the_region_inside_the_single_cluster_mapping():
    """A second `cluster:` key would silently shadow `id` and `hubUrl`."""
    out = _setup_values(values(region="ap-southeast-1"))
    lines = out.splitlines()
    assert len([line for line in lines if line == "cluster:"]) == 1
    assert "  region: ap-southeast-1" in lines
    assert lines.index("  region: ap-southeast-1") < lines.index("monitoring:")


def test_setup_values_enable_monitoring_with_the_configured_url():
    out = _setup_values(values(remote_write_url="https://metrics.example/api/v1/write"))
    assert "monitoring:" in out
    assert "  enabled: true" in out
    assert "  remoteWrite:" in out
    assert "    url: https://metrics.example/api/v1/write" in out


def test_setup_values_disable_monitoring_when_no_url_is_configured():
    out = _setup_values(values())
    assert "monitoring:" in out
    assert "  enabled: false" in out
    assert "remoteWrite:" not in out


def test_setup_values_never_render_a_remote_write_credential():
    """The chart derives basicAuth from the enrolment-token Secret it already creates."""
    url = "https://metrics.example/api/v1/write"
    out = _setup_values(values(remote_write_url=url, enrolment_token="hgro1.secret"))
    assert "basicAuth" not in out
    assert "password" not in out
    assert "username" not in out
    assert out.count("hgro1.secret") == 1


def test_setup_values_render_the_one_gateway_address():
    """One address, never a list of replicas: scaling the gateway must not reach a cluster."""
    out = _setup_values(values(gateway="tunnel.example:443"))
    lines = out.splitlines()
    assert "agent:" in lines
    assert "  gateway:" in lines
    assert "    address: tunnel.example:443" in lines
    assert "gateways" not in out


def test_setup_values_render_the_agent_off_even_with_a_gateway():
    """The chart refuses an agent on a readOnly cluster, and readOnly is the default."""
    out = _setup_values(values(gateway="tunnel.example:443"))
    lines = out.splitlines()
    assert lines.index("agent:") < lines.index("  enabled: false")


def test_setup_values_leave_the_gateway_blank_when_none_is_configured():
    out = _setup_values(values())
    assert '  gateway: ""' in out


def test_setup_values_pin_the_agent_image_only_alongside_a_gateway():
    """An image with nowhere to dial is an agent that cannot establish anything."""
    without = _setup_values(values(agent_image="ghcr.io/x/agent:sha7"))
    assert "ghcr.io/x/agent" not in without
    with_gateway = _setup_values(values(agent_image="ghcr.io/x/agent:sha7", gateway="tunnel.example:443"))
    assert "    repository: ghcr.io/x/agent" in with_gateway
    assert "    tag: sha7" in with_gateway


def test_setup_values_carry_the_editable_infra_skeleton():
    out = _setup_values(values())
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
    assert schema.headroom == ClusterHeadroomSchema(free_devices=0, whole_nodes_free=0, largest_contiguous_block=0)


def test_free_block_is_floored_at_one_when_devices_are_free_but_domainless():
    schema = _rollup(new_uid(CLUSTER), {"devices_total": 4, "devices_allocatable": 2, "largest_block": 0})
    assert schema.headroom.largest_contiguous_block == 1


def test_domain_block_is_used_when_present():
    schema = _rollup(new_uid(CLUSTER), {"devices_total": 8, "devices_allocatable": 8, "largest_block": 4})
    assert schema.headroom.largest_contiguous_block == 4
