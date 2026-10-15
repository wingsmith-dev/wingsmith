import inspect
import datetime as dt

from fnmatch import fnmatch
from pathlib import Path

import yaml
import pytest
import msgspec

from app.config import settings
from app.core.ids import CLUSTER
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.modules.clusters.routes import ClusterRoutes
from app.modules.clusters.schemas import ClusterRollupSchema
from app.modules.clusters.schemas import ClusterCreateRequest
from app.modules.clusters.schemas import ClusterUpdateRequest
from app.modules.clusters.schemas import ClusterHeadroomSchema
from app.modules.clusters.services import SetupValues
from app.modules.clusters.services import ClusterService
from app.modules.clusters.services import _rollup
from app.modules.clusters.services import _hub_url
from app.modules.clusters.services import _revision
from app.modules.clusters.services import _setup_values
from app.modules.clusters.services import _registry_host
from app.modules.clusters.services import values_filename
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


def test_hub_url_is_the_configured_one(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "wss://hub.example/v1/hub/sync")
    monkeypatch.setattr(settings, "PUBLIC_HOST", "elsewhere.example")
    assert _hub_url() == "wss://hub.example/v1/hub/sync"


def test_hub_url_is_refused_when_unset_rather_than_guessed(monkeypatch):
    """The request's Host header is the caller's to choose, so it is never a fallback."""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_HOST", "hangar.example")
    with pytest.raises(Invalid) as refused:
        _hub_url()
    assert refused.value.detail == {"setting": "HANGAR_HUB_PUBLIC_URL"}


def test_hub_url_defaults_to_the_compose_hub_under_local(monkeypatch):
    """`just dev` and `just fleet` set no hub URL; local falls back to settings, never the request."""
    monkeypatch.setattr(settings, "APP_ENV", "local")
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_HOST", "attacker.example")
    assert _hub_url() == f"ws://localhost:{settings.HUB_PORT}{settings.HUB_PATH}"


def test_hub_url_must_be_a_websocket_url(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "https://hub.example")
    with pytest.raises(Invalid):
        _hub_url()


class FakeClusterRepo:
    def __init__(self, cluster) -> None:
        self.cluster = cluster

    async def get(self, ref):
        return self.cluster


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        return None


class FakeCluster:
    def __init__(self, name="alpha", region="eu-west-1", token_hash=None) -> None:
        self.name = name
        self.region = region
        self.token_hash = token_hash
        self.remote_write_hash = None


def setup_service(cluster) -> ClusterService:
    svc = ClusterService.__new__(ClusterService)
    svc.db = FakeSession()
    svc.repo = FakeClusterRepo(cluster)
    return svc


async def test_minting_without_a_hub_url_writes_nothing(monkeypatch):
    """Refusing after the rotation would strand a working cluster on a dead token."""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "")
    cluster = FakeCluster(token_hash="existing")
    svc = setup_service(cluster)
    with pytest.raises(Invalid):
        await svc.issue_enrolment_token("alpha")
    assert cluster.token_hash == "existing"
    assert svc.db.commits == 0


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(settings, "HUB_PUBLIC_URL", "wss://hub.example/v1/hub/sync")
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_NAME", "registry-pull")
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_USERNAME", "machine-user")
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_TOKEN", "fleet-registry-token")
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_SHARE_TOKEN", False)


async def test_the_fleet_registry_token_is_never_on_a_plain_read(hub, monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_SHARE_TOKEN", True)
    setup = await setup_service(FakeCluster()).setup_instructions("alpha")
    assert "fleet-registry-token" not in setup.values_yaml
    assert yaml.safe_load(setup.values_yaml)["imagePullSecret"]["token"] == ""


async def test_the_fleet_registry_token_is_not_shared_at_mint_by_default(hub):
    """The cluster is asked for a read-only token of its own instead."""
    setup = await setup_service(FakeCluster()).issue_enrolment_token("alpha")
    assert "fleet-registry-token" not in setup.values_yaml
    secret = yaml.safe_load(setup.values_yaml)["imagePullSecret"]
    assert secret == {
        "create": True,
        "name": "registry-pull",
        "registry": "ghcr.io",
        "username": "machine-user",
        "token": "",
    }
    assert "read-only token for THIS" in setup.values_yaml


async def test_the_fleet_registry_token_is_shared_at_mint_only_when_opted_in(hub, monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_PULL_SECRET_SHARE_TOKEN", True)
    setup = await setup_service(FakeCluster()).issue_enrolment_token("alpha")
    assert yaml.safe_load(setup.values_yaml)["imagePullSecret"]["token"] == "fleet-registry-token"


async def test_the_values_file_is_named_after_the_cluster(hub):
    """A bare values.yaml saved next to the chart overwrites the chart's own."""
    setup = await setup_service(FakeCluster(name="alpha")).setup_instructions("alpha")
    assert setup.values_filename == "values.alpha.local.yaml"
    assert "-f values.alpha.local.yaml" in setup.helm_command
    assert "-f values.yaml" not in setup.helm_command


async def test_the_helm_command_quotes_a_hostile_cluster_name(hub):
    """An older row the schema would now refuse still must not add a flag or a command."""
    setup = await setup_service(FakeCluster(name="x; rm -rf ~")).setup_instructions("x")
    assert "--kube-context 'x; rm -rf ~'" in setup.helm_command
    assert "-f 'values.x; rm -rf ~.local.yaml'" in setup.helm_command


def test_values_filename():
    assert values_filename("eu-west-1a") == "values.eu-west-1a.local.yaml"


@pytest.mark.parametrize("ignore_file", [".gitignore", "libs/chart/cluster-setup/.helmignore"])
def test_the_values_file_is_ignored_by_git_and_helm(ignore_file):
    """The download carries the enrolment token; saved in a checkout it must not be committed or packaged."""
    root = Path(__file__).resolve().parents[3]
    patterns = [
        line.strip().lstrip("/")
        for line in (root / ignore_file).read_text().splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    assert any(fnmatch(values_filename("eu-west-1a"), p) for p in patterns)


def values(**overrides) -> SetupValues:
    fields = {"cluster_name": "cid", "hub_url": "wss://h/v1/hub/sync", "region": "us-east-1"}
    return SetupValues(**{**fields, **overrides})


def test_setup_values_carry_identity_and_a_pinned_image():
    out = _setup_values(values(operator_image="ghcr.io/x/operator:sha7"))
    parsed = yaml.safe_load(out)
    assert parsed["cluster"]["id"] == "cid"
    assert parsed["cluster"]["hubUrl"] == "wss://h/v1/hub/sync"
    assert parsed["operator"]["image"] == {"repository": "ghcr.io/x/operator", "tag": "sha7"}


def test_setup_values_parse_back_to_what_was_filled_in():
    out = _setup_values(values(gateway="tunnel.example:443", remote_write_url="https://m.example/write"))
    parsed = yaml.safe_load(out)
    assert parsed["agent"] == {"enabled": False, "gateway": {"address": "tunnel.example:443"}}
    assert parsed["monitoring"] == {"enabled": True, "remoteWrite": {"url": "https://m.example/write"}}
    assert parsed["frontend"] == {"mode": "none"}


def test_setup_values_cannot_be_given_extra_keys_by_a_value():
    """Each value is serialised, never spliced: a newline in a region is a string, not a key."""
    out = _setup_values(values(region="eu\nreadOnly: true\nevil: 1", cluster_name="cid\n  token: x"))
    parsed = yaml.safe_load(out)
    assert parsed["cluster"]["region"] == "eu\nreadOnly: true\nevil: 1"
    assert parsed["cluster"]["id"] == "cid\n  token: x"
    assert parsed["cluster"]["readOnly"] is False
    assert "evil" not in parsed


def test_setup_values_keep_a_numeric_looking_tag_a_string():
    parsed = yaml.safe_load(_setup_values(values(operator_image="ghcr.io/x/operator:1.10")))
    assert parsed["operator"]["image"]["tag"] == "1.10"


def test_setup_values_skip_an_image_with_no_tag():
    """`registry:5000/operator` has a port, not a tag."""
    assert "operator:" not in _setup_values(values(operator_image="registry:5000/operator"))


def test_setup_values_register_a_cluster_that_reconciles():
    """Read-only is not the registration default, and the reason is a silent stall.

    A read-only operator enrols, reports, stays healthy and applies nothing. The
    plane cannot see that except through `cluster.observed.read_only`, so a
    cluster installed inert looks identical to a working one until somebody
    notices a workload that never starts.
    """
    out = _setup_values(values())
    assert "readOnly: false" in out
    assert "readOnly: true" not in out


def test_setup_values_leave_the_agent_off_for_the_first_install():
    """Not because of `readOnly` — because the frontend's CRD is applied at runtime.

    The chart refuses an agent with no frontend to forward to, and the frontend
    renders a DynamoGraphDeployment whose CRD does not exist until the Dynamo
    operator has run. Both go on together in a later upgrade.
    """
    out = _setup_values(values(gateway="tunnel.example:443"))
    assert "enabled: false" in out
    assert "readOnly` above is true" not in out


def test_setup_values_omit_image_when_unset():
    out = _setup_values(values())
    assert "operator:" not in out


def test_setup_values_fold_in_the_enrolment_token_only_when_given():
    without = _setup_values(values())
    assert "enrolmentToken:" not in without
    with_token = _setup_values(values(enrolment_token="hgro1.secret"))
    assert yaml.safe_load(with_token)["enrolmentToken"] == {"value": "hgro1.secret"}


def test_setup_values_render_a_shared_pull_secret_when_one_is_given():
    """Only reached when the plane opted in to sharing its token, at mint."""
    out = _setup_values(
        values(
            pull_secret_name="registry-pull",
            pull_secret_registry="ghcr.io",
            pull_secret_username="machine-user",
            pull_secret_token="tok_x",
        )
    )
    assert yaml.safe_load(out)["imagePullSecret"] == {
        "create": True,
        "name": "registry-pull",
        "registry": "ghcr.io",
        "username": "machine-user",
        "token": "tok_x",
    }


def test_setup_values_omit_the_pull_secret_when_none_is_configured():
    """The default. Public images, or a registry the nodes already authenticate to."""
    assert "imagePullSecret" not in _setup_values(values())


def test_setup_values_leave_a_named_pull_secret_blank_for_the_clusters_own_token():
    """A name with no token is the default private-image setup, not a half-configured one."""
    out = _setup_values(values(pull_secret_name="registry-pull", pull_secret_registry="ghcr.io"))
    lines = out.splitlines()
    secret = yaml.safe_load(out)["imagePullSecret"]
    assert secret["create"] is True
    assert secret["token"] == ""
    assert lines.index("imagePullSecret:") > next(i for i, line in enumerate(lines) if "Edit below" in line)


def test_setup_values_never_render_a_token_without_a_name():
    assert "imagePullSecret" not in _setup_values(values(pull_secret_token="tok_x"))


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
    assert yaml.safe_load(out)["monitoring"] == {
        "enabled": True,
        "remoteWrite": {"url": "https://metrics.example/api/v1/write"},
    }


def test_setup_values_disable_monitoring_when_no_url_is_configured():
    out = _setup_values(values())
    assert yaml.safe_load(out)["monitoring"] == {"enabled": False}


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


def test_setup_values_leave_the_gateway_blank_in_the_nested_shape():
    """A flat `gateway: ""` is a string where the chart has a map — Helm refuses it outright."""
    out = _setup_values(values())
    assert "  gateway:" in out.splitlines()
    assert yaml.safe_load(out)["agent"]["gateway"] == {"address": ""}


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


def test_device_rollup_does_not_count_a_held_device_as_free(repo):
    """A live allocation takes a device out of the free count.

    `allocatable` alone says the card is claimable, not unclaimed, and a rollup
    that read only the column reported every allocated device as free.
    """
    text = sql(repo.device_rollup_query([1]))
    assert "bind_allocation_device.released_at IS NULL" in text
    assert "NOT (EXISTS" in text


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
    assert "bind_allocation_device.released_at IS NULL" in text
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


# --- what a request may carry ----------------------------------------------------


def decode(body: dict, type_=ClusterCreateRequest):
    return msgspec.json.decode(msgspec.json.encode(body), type=type_)


@pytest.mark.parametrize(
    "name",
    ["Alpha", "alpha beta", "alpha\n", "-alpha", "a" * 64, "", "alpha;rm", "alpha_1", "alpha.example"],
)
def test_a_cluster_name_that_is_not_a_dns_label_is_refused(name):
    """The name becomes the chart's `cluster.id`, a kube context in a shell line, and a metrics label."""
    with pytest.raises(msgspec.ValidationError):
        decode({"name": name, "region": "eu-west-1"})


@pytest.mark.parametrize("region", ["eu west", "eu-west-1\n", "x" * 64, "", "eu:west"])
def test_a_region_that_is_not_a_label_value_is_refused(region):
    with pytest.raises(msgspec.ValidationError):
        decode({"name": "alpha", "region": region})
    with pytest.raises(msgspec.ValidationError):
        decode({"region": region}, ClusterUpdateRequest)


def test_a_well_formed_cluster_is_accepted():
    data = decode({"name": "eu-west-1a", "region": "eu-west-1", "zone": "eu-west-1a", "metrics_label": ""})
    assert (data.name, data.region, data.metrics_label) == ("eu-west-1a", "eu-west-1", "")


def test_an_unbounded_display_name_is_refused():
    with pytest.raises(msgspec.ValidationError):
        decode({"name": "alpha", "region": "eu-west-1", "display_name": "x" * 201})


@pytest.mark.parametrize("type_", [ClusterCreateRequest, ClusterUpdateRequest])
def test_an_unknown_phase_is_refused_when_decoded(type_):
    with pytest.raises(msgspec.ValidationError):
        decode({"name": "alpha", "region": "eu-west-1", "phase": "retired"}, type_)


@pytest.mark.parametrize(
    "extra",
    [
        {"labels": {"tier": "x" * 64}},
        {"labels": {"k" * 254: "gold"}},
        {"labels": {"tier": 1}},
        {"labels": {f"k{i}": "v" for i in range(65)}},
        {"capabilities": {"x" * 64: True}},
        {"capabilities": {f"k{i}": True for i in range(65)}},
    ],
)
def test_unbounded_labels_and_capabilities_are_refused(extra):
    """Both land in JSONB untouched, so the request is the only place they are bounded."""
    with pytest.raises(msgspec.ValidationError):
        decode({"name": "alpha", "region": "eu-west-1", **extra})
    with pytest.raises(msgspec.ValidationError):
        decode(extra, ClusterUpdateRequest)


def test_prefixed_labels_and_nested_capabilities_are_accepted():
    data = decode(
        {
            "name": "alpha",
            "region": "eu-west-1",
            "phase": "active",
            "labels": {"hangar.io/alert.severity": "warning", "tier": ""},
            "capabilities": {"rdma": {"fabric": "ib", "gbps": 400}},
        }
    )
    assert data.phase == "active"
    assert data.labels["tier"] == ""
    assert data.capabilities["rdma"]["gbps"] == 400


@pytest.mark.parametrize("name", ["rollups", "tunnel-auth"])
async def test_a_cluster_cannot_take_a_route_segment_as_its_name(name):
    """`GET /v1/clusters/rollups` is the rollup list, so a cluster called that is unreachable by name."""
    with pytest.raises(Invalid):
        await ClusterService(db=None).create(ClusterCreateRequest(name=name, region="eu-west-1"))


# --- delete ----------------------------------------------------------------------


class DeletableCluster(FakeCluster):
    def __init__(self) -> None:
        super().__init__(name="alpha")
        self.id = "row-id"
        self.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.updated_at = None


class DeleteRepo(FakeClusterRepo):
    def __init__(self, cluster, holds=None) -> None:
        super().__init__(cluster)
        self.held = holds or {"deployments": 0, "allocations": 0, "pinned_workloads": 0}
        self.deleted = []

    async def holds(self, cluster_id):
        return self.held

    async def delete(self, cluster):
        self.deleted.append(cluster)


def delete_service(holds=None):
    cluster = DeletableCluster()
    svc = ClusterService.__new__(ClusterService)
    svc.db = FakeSession()
    svc.repo = DeleteRepo(cluster, holds)
    return svc, cluster


async def test_a_delete_without_if_match_or_force_is_refused():
    svc, cluster = delete_service()
    with pytest.raises(Invalid) as refused:
        await svc.delete("alpha")
    assert refused.value.detail == {"revision": _revision(cluster)}
    assert svc.repo.deleted == []


async def test_a_delete_against_a_stale_revision_is_refused():
    svc, _ = delete_service()
    with pytest.raises(Conflict):
        await svc.delete("alpha", if_match="12345")
    assert svc.repo.deleted == []


@pytest.mark.parametrize("held", ["deployments", "allocations", "pinned_workloads"])
async def test_a_cluster_with_anything_bound_to_it_refuses_even_when_forced(held):
    holds = {"deployments": 0, "allocations": 0, "pinned_workloads": 0, held: 2}
    svc, _ = delete_service(holds)
    with pytest.raises(Conflict) as refused:
        await svc.delete("alpha", force=True)
    assert refused.value.detail == holds
    assert svc.repo.deleted == []


async def test_an_empty_cluster_is_deleted_with_its_revision_or_with_force():
    svc, cluster = delete_service()
    await svc.delete("alpha", if_match=_revision(cluster))
    assert svc.repo.deleted == [cluster]

    svc, cluster = delete_service()
    await svc.delete("alpha", force=True)
    assert svc.repo.deleted == [cluster]


def test_the_holds_query_counts_deployments_live_allocations_and_pins(repo):
    text = sql(repo.holds_query("row-id"))
    assert "count(demand_deployment.id)" in text
    assert "bind_allocation.released_at IS NULL" in text
    assert "demand_workload.pinned_cluster_id" in text


def test_delete_route_takes_if_match_and_force():
    parameters = set(inspect.signature(ClusterRoutes.delete_cluster.fn).parameters)
    assert {"if_match", "force"} <= parameters
