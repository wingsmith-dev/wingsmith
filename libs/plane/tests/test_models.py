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
    "demand_artifact_baseline",
    "demand_workload",
    "demand_workload_role",
    "demand_workload_revision",
    "demand_deployment",
    "demand_deployment_status",
    "demand_discovered_workload",
    "demand_workload_adoption",
    "demand_replica",
    "demand_endpoint",
    "demand_endpoint_alias",
    "demand_endpoint_target",
    "demand_warm_set",
}

BINDING = {
    "bind_tenant",
    "bind_tenant_token",
    "bind_tenant_api_key",
    "bind_project",
    "bind_quota",
    "bind_serving_quota",
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
    "ops_benchmark_request",
    "ops_event",
    "ops_cache_entry",
    "ops_credential",
    "ops_alert",
    "ops_alert_threshold",
    "ops_notification_channel",
    "ops_reactor_state",
    "hub_cluster_claim",
}

AUTH = {
    "auth_refresh_token",
}

EXPECTED = SUPPLY | DEMAND | BINDING | MACHINERY | AUTH


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


def test_workload_requires_a_device_class() -> None:
    """A count without a class is not a shape: you cannot tensor-parallel across two models."""
    from app.db.models import Workload

    assert inspect(Workload).columns["device_class"].nullable is False
    assert inspect(Workload).columns["context_length"].nullable is False


def test_the_workload_carries_the_serving_configuration_itself() -> None:
    """There is no `Recipe` to point at, and no `recipe_id` left pointing at one."""
    from app.db.models import Workload

    columns = set(inspect(Workload).columns.keys())
    assert {"engine", "engine_runtime_version", "engine_args", "context_length", "serving_digest"} <= columns
    assert {"tensor_parallel", "pipeline_parallel", "expert_parallel"} <= columns
    assert {"artifact_id", "capabilities", "imported_args", "overlay"} <= columns
    assert not columns & {"recipe_id", "recipe_version"}


def test_the_topology_pair_is_one_answer_and_not_an_override() -> None:
    """It was nullable while it overrode a recipe's value. There is nothing to override now."""
    from app.db.models import Workload

    assert inspect(Workload).columns["requires_single_topology_domain"].nullable is False


def test_workload_carries_the_discriminator_and_the_global_declaration() -> None:
    """Admission is not here: it is per-cluster and lives on the deployment."""
    from app.db.models import Workload

    columns = set(inspect(Workload).columns.keys())
    assert {"kind", "priority_class_id", "gang_size", "artifact_id"} <= columns
    assert {"min_scale", "max_scale", "desired_scale", "strategy", "max_clusters"} <= columns
    assert not columns & {"admission_phase", "admission_reason", "cluster_id", "queued_at", "placed_at"}


def test_a_deployment_carries_the_per_cluster_facts() -> None:
    from app.db.models import Deployment

    columns = set(inspect(Deployment).columns.keys())
    assert {"cluster_id", "admission_phase", "admission_reason", "queued_at", "admitted_at", "placed_at"} <= columns
    assert not columns & {"serving_digest", "overrides", "labels", "adapter_artifact_ids"}


def test_a_workload_has_many_deployments_and_not_one() -> None:
    """The FK flip: `workload_id` lost its unique constraint, which is the whole change."""
    from app.db.models import Deployment

    column = inspect(Deployment).columns["workload_id"]
    assert column.unique is not True
    assert column.index is True


def test_serving_quota_is_traffic_and_shares_no_field_with_device_quota() -> None:
    """Two objects called quota, and `over quota` has to keep meaning one thing."""
    from app.db.models import Quota
    from app.db.models import ServingQuota

    serving = set(inspect(ServingQuota).columns.keys())
    devices = set(inspect(Quota).columns.keys())
    assert {"committed_rpm", "committed_tpm", "max_concurrent_requests", "burst_rpm"} <= serving
    assert serving & devices == {"created_at", "updated_at", "seq", "id", "uid"}
    assert not serving & {"guaranteed_devices", "limit_devices", "device_class"}


def test_serving_quota_is_one_per_endpoint() -> None:
    from app.db.models import ServingQuota

    column = inspect(ServingQuota).columns["endpoint_id"]
    assert column.nullable is False
    assert column.unique is True


def test_serving_quota_declares_its_multi_replica_enforcement_mode() -> None:
    from app.db.models import ServingQuota

    column = inspect(ServingQuota).columns["limit_scope"]
    assert column.nullable is False
    assert column.server_default.arg == "per_replica"


def test_serving_quota_carries_no_observed_field() -> None:
    """`enforced` is a shadow-mode switch, not a phase, and usage is measured elsewhere."""
    from app.db.models import ServingQuota

    columns = set(inspect(ServingQuota).columns.keys())
    assert "enforced" in columns
    assert not columns & {"current_rpm", "last_throttled_at", "breaches", "phase", "state"}


def test_alert_threshold_scope_is_unique_with_nulls_not_distinct() -> None:
    """A nullable scope column in a plain UNIQUE index admits duplicate fleet defaults."""
    from app.db.models import AlertThreshold

    table = inspect(AlertThreshold).local_table
    index = next(i for i in table.indexes if i.name == "uq_ops_alert_threshold_scope")
    assert index.unique is True
    assert index.dialect_options["postgresql"]["nulls_not_distinct"] is True
    assert [c.name for c in index.columns] == ["rule", "subject_type", "subject_uid", "dimensions"]
    assert table.columns["subject_uid"].nullable is True
    assert not table.columns["subject_uid"].foreign_keys


def test_a_fleet_wide_quota_or_credential_is_unique_in_its_scope() -> None:
    """A null `cluster_id` or `scope_id` is one scope, not a fresh one per row."""
    from app.db.models import Quota
    from app.db.models import Credential

    for model, name, columns in (
        (Quota, "uq_bind_quota_scope", ["project_id", "cluster_id", "device_class"]),
        (Credential, "uq_ops_credential_scope_name", ["scope_type", "scope_id", "name"]),
    ):
        table = inspect(model).local_table
        constraint = next(c for c in table.constraints if c.name == name)
        assert [c.name for c in constraint.columns] == columns
        assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_seq_is_a_bigint_identity_that_does_not_cycle() -> None:
    from sqlalchemy import BigInteger

    from app.db.models import Event

    column = inspect(Event).local_table.columns["seq"]
    assert isinstance(column.type, BigInteger)
    assert not column.identity.cycle


def test_benchmark_request_is_declared_and_holds_no_phase_or_verdict() -> None:
    """A request is declared, a run's phase is observed, a verdict is derived at read."""
    from app.db.models import BenchmarkRequest

    columns = set(inspect(BenchmarkRequest).columns.keys())
    assert {"level", "mode", "criteria_version", "coverage", "homogeneous"} <= columns
    assert not columns & {"phase", "summary", "verdict", "started_at", "finished_at", "vantage"}


def test_benchmark_request_targets_exactly_one_level() -> None:
    from app.db.models import BenchmarkRequest

    table = inspect(BenchmarkRequest).local_table
    names = {c.name for c in table.constraints}
    assert "ck_ops_benchmark_request_one_target" in names
    for column in ("deployment_id", "workload_id", "endpoint_id"):
        assert table.columns[column].nullable is True


def test_benchmark_run_hangs_off_a_request_and_owns_a_deployment_or_an_endpoint() -> None:
    from app.db.models import BenchmarkRun

    table = inspect(BenchmarkRun).local_table
    assert {"request_id", "endpoint_id", "step_index", "discard", "serving"} <= set(table.columns.keys())
    assert table.columns["deployment_id"].nullable is True
    assert table.columns["cluster_id"].nullable is True
    assert "ck_ops_benchmark_run_owner" in {c.name for c in table.constraints}


def test_artifact_baseline_is_unique_per_dataset_and_metric() -> None:
    from app.db.models import ArtifactBaseline

    table = inspect(ArtifactBaseline).local_table
    constraint = next(c for c in table.constraints if c.name == "uq_demand_artifact_baseline_dataset_metric")
    assert [c.name for c in constraint.columns] == ["artifact_id", "dataset", "metric"]
    assert {"source_uri", "recorded_by", "recorded_at"} <= set(table.columns.keys())


def test_the_serving_columns_are_not_null_with_an_empty_default() -> None:
    """No reader should have to choose between `is None` and `== {}`."""
    from app.db.models import Artifact
    from app.db.models import Endpoint
    from app.db.models import Workload

    for model, column, default in (
        (Workload, "serving", "{}"),
        (Workload, "capabilities", "{}"),
        (Endpoint, "surface", "{}"),
        (Artifact, "upstream", "{}"),
    ):
        mapped = inspect(model).columns[column]
        assert mapped.nullable is False, column
        assert mapped.server_default.arg == default, column


def test_the_four_new_id_prefixes_are_registered() -> None:
    from app.core import ids

    assert {ids.SERVING_QUOTA, ids.BENCHMARK_REQUEST, ids.ARTIFACT_BASELINE, ids.ALERT_THRESHOLD} <= ids.PREFIXES
    for prefix in (ids.SERVING_QUOTA, ids.BENCHMARK_REQUEST, ids.ARTIFACT_BASELINE, ids.ALERT_THRESHOLD):
        assert ids.is_uid(ids.new_uid(prefix))
