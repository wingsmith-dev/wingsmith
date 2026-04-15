import importlib

from sqlalchemy import inspect

from app.db.engine import Base


SUPPLY = {
    "supply_cluster",
    "supply_cluster_serving",
    "supply_cluster_sync_state",
    "supply_node",
    "supply_device",
    "supply_topology_domain",
    "supply_topology_domain_node",
    "supply_pool",
}

DEMAND = {
    "demand_artifact",
    "demand_artifact_location",
    "demand_recipe",
    "demand_workload",
    "demand_deployment",
    "demand_deployment_status",
    "demand_discovered_workload",
    "demand_workload_adoption",
    "demand_replica",
    "demand_endpoint",
    "demand_endpoint_target",
    "demand_warm_set",
}

BINDING = {
    "bind_tenant",
    "bind_project",
    "bind_quota",
    "bind_priority_class",
    "bind_policy",
    "bind_policy_attachment",
    "bind_allocation",
    "bind_allocation_device",
}

MACHINERY = {
    "ops_component",
    "ops_component_status",
    "ops_operation",
    "ops_benchmark_run",
    "ops_event",
    "ops_cache_entry",
    "ops_credential",
    "ops_alert",
    "ops_notification_channel",
    "ops_reactor_state",
    "hub_cluster_claim",
}

EXPECTED = SUPPLY | DEMAND | BINDING | MACHINERY


def test_model_layer_imports_cleanly() -> None:
    importlib.import_module("app.db.models")


def test_metadata_contains_every_expected_table() -> None:
    importlib.import_module("app.db.models")
    assert EXPECTED <= set(Base.metadata.tables)


def test_metadata_contains_no_unexpected_tables() -> None:
    importlib.import_module("app.db.models")
    assert set(Base.metadata.tables) == EXPECTED


def test_every_table_has_the_base_mixin_columns() -> None:
    importlib.import_module("app.db.models")
    for name, table in Base.metadata.tables.items():
        columns = set(table.columns.keys())
        assert {"created_at", "updated_at", "seq"} <= columns, name
        assert "id" in columns, name


def test_node_has_no_device_count_and_no_orchestrator_version() -> None:
    from app.db.models import Node

    columns = set(inspect(Node).columns.keys())
    assert "gpu_count" not in columns
    assert "device_count" not in columns
    assert "kubelet_version" not in columns


def test_cluster_has_no_address_and_reachability_is_separate() -> None:
    from app.db.models import Cluster
    from app.db.models import ClusterSyncState

    cluster_columns = set(inspect(Cluster).columns.keys())
    assert "api_endpoint" not in cluster_columns
    assert "reachable" not in cluster_columns
    assert "reachable" in set(inspect(ClusterSyncState).columns.keys())


def test_no_telemetry_columns_anywhere() -> None:
    importlib.import_module("app.db.models")
    banned = {"utilization", "temperature", "throughput", "latency", "token_count", "power_watts"}
    for name, table in Base.metadata.tables.items():
        for column in list(table.columns.keys()):
            assert not any(term in column for term in banned), f"{name}.{column}"


def test_recipe_requires_a_device_class() -> None:
    from app.db.models import Recipe

    assert inspect(Recipe).columns["device_class"].nullable is False
    assert inspect(Recipe).columns["context_length"].nullable is False


def test_workload_carries_the_discriminator_and_the_global_declaration() -> None:
    """Admission is not here: it is per-cluster and lives on the deployment."""
    from app.db.models import Workload

    columns = set(inspect(Workload).columns.keys())
    assert {"kind", "priority_class_id", "gang_size", "recipe_id"} <= columns
    assert {"min_scale", "max_scale", "desired_scale", "strategy", "max_clusters"} <= columns
    assert not columns & {"admission_phase", "admission_reason", "cluster_id", "queued_at", "placed_at"}


def test_a_deployment_carries_the_per_cluster_facts() -> None:
    from app.db.models import Deployment

    columns = set(inspect(Deployment).columns.keys())
    assert {"cluster_id", "admission_phase", "admission_reason", "queued_at", "admitted_at", "placed_at"} <= columns
    assert not columns & {"recipe_id", "overrides", "labels", "adapter_artifact_ids"}


def test_a_workload_has_many_deployments_and_not_one() -> None:
    """The FK flip: `workload_id` lost its unique constraint, which is the whole change."""
    from app.db.models import Deployment

    column = inspect(Deployment).columns["workload_id"]
    assert column.unique is not True
    assert column.index is True
