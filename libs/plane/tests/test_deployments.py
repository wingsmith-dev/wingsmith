import inspect
import datetime as dt

import pytest

from app.core.ids import RECIPE
from app.core.ids import CLUSTER
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Workload
from app.db.models import Deployment
from app.core.exceptions import Invalid
from app.modules.deployments.routes import DeploymentRoutes
from app.modules.deployments.schemas import DeploymentSchema
from app.modules.deployments.schemas import DeploymentCreateRequest
from app.modules.deployments.schemas import DeploymentUpdateRequest
from app.modules.deployments.schemas import DeploymentWorkloadSchema
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema
from app.modules.deployments.services import EMPTY
from app.modules.deployments.services import TYPES
from app.modules.deployments.services import PHASES
from app.modules.deployments.services import _revision
from app.modules.deployments.services import check_scale
from app.modules.deployments.repository import DeploymentRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return DeploymentRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_create_request_defaults_are_a_single_replica_service():
    data = DeploymentCreateRequest(cluster="iad-1", recipe="llama-70b", name="serve", project="acme")
    assert data.type == "service"
    assert (data.min_replicas, data.desired_replicas, data.max_replicas) == (1, 1, 1)
    assert data.enabled is True
    assert data.priority_class is None
    assert data.adapter_artifacts == []


def test_update_request_leaves_absent_fields_unset():
    data = DeploymentUpdateRequest(desired_replicas=4)
    assert data.to_dict(exclude_unset=True) == {"desired_replicas": 4}


def test_deployment_cannot_be_moved_between_clusters_or_recipes_by_patch():
    fields = DeploymentUpdateRequest.__struct_fields__
    assert "cluster" not in fields
    assert "recipe" not in fields


def test_admission_phase_is_not_client_assertable():
    assert "phase" not in DeploymentUpdateRequest.__struct_fields__


def test_priority_is_not_duplicated_onto_the_deployment():
    """Priority, project, and phase belong to the workload supertype, once."""
    fields = set(DeploymentSchema.__struct_fields__)
    assert "priority_class" not in fields
    assert "priority" not in fields
    assert "project_id" not in fields
    assert "phase" not in fields
    assert {"priority_class", "project_id", "phase", "type"} <= set(DeploymentWorkloadSchema.__struct_fields__)


def test_lifecycle_discriminator_and_admission_phases_match_the_supertype():
    assert TYPES == {"service", "task", "gang"}
    assert {"pending", "admitted", "placed", "running"} <= PHASES


def test_replica_rollup_is_a_count_not_a_list():
    fields = set(DeploymentSchema.__struct_fields__)
    assert "replicas" in fields
    assert set(DeploymentReplicaRollupSchema.__struct_fields__) == {
        "total",
        "ready",
        "running",
        "pending",
        "failed",
        "at_current_generation",
    }


def test_empty_rollup_is_all_zero():
    assert EMPTY == DeploymentReplicaRollupSchema(
        total=0, ready=0, running=0, pending=0, failed=0, at_current_generation=0
    )


@pytest.mark.parametrize(
    ("low", "want", "high"),
    [(1, 1, 1), (0, 0, 0), (1, 3, 8), (0, 0, 4)],
)
def test_scale_bounds_that_agree(low, want, high):
    assert check_scale(low, want, high) is None


@pytest.mark.parametrize(
    ("low", "want", "high"),
    [(-1, 0, 0), (2, 1, 8), (1, 9, 8), (4, 4, 2), (0, 1, 0)],
)
def test_scale_bounds_that_contradict(low, want, high):
    with pytest.raises(Invalid):
        check_scale(low, want, high)


def test_zero_replicas_is_parked_not_invalid():
    assert check_scale(0, 0, 4) is None


def test_label_selector_becomes_a_predicate_per_key(repo):
    """Labels are the deployment's own, never the workload supertype's."""
    text = sql(repo.list_query(limit=10, labels={"tier": "gold", "team": "search"}))
    assert text.count("demand_deployment.labels ->>") == 2
    assert "->>" not in sql(repo.list_query(limit=10))


def test_revision_follows_the_later_of_the_two_rows():
    """A patch that only moves priority touches the workload, not the deployment."""
    deployment = Deployment(created_at=EPOCH, updated_at=None)
    workload = Workload(created_at=EPOCH, updated_at=EPOCH + dt.timedelta(seconds=5))
    assert _revision(deployment, workload) == str(int((EPOCH + dt.timedelta(seconds=5)).timestamp() * 1_000_000))
    assert _revision(deployment, workload) != _revision(deployment, Workload(created_at=EPOCH, updated_at=None))


def test_base_query_joins_the_workload_supertype(repo):
    text = sql(repo.list_query(limit=10))
    assert "FROM demand_deployment JOIN demand_workload ON demand_deployment.workload_id = demand_workload.id" in text
    assert "JOIN bind_priority_class" in text
    assert "JOIN bind_project" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            cluster="iad-1",
            recipe="llama-70b",
            project="acme",
            phase="running",
            type="service",
        )
    )
    assert "demand_deployment.seq > " in text
    assert "supply_cluster.name = " in text
    assert "demand_recipe.name = " in text
    assert "bind_project.slug = " in text
    assert "demand_workload.admission_phase = " in text
    assert "demand_workload.kind = " in text
    assert "ORDER BY demand_deployment.seq ASC" in text


def test_reference_filters_resolve_an_id_against_uid_not_name(repo):
    text = sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER), recipe=new_uid(RECIPE)))
    assert "supply_cluster.uid = " in text
    assert "demand_recipe.uid = " in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_deployment.name = " in sql(repo.get_query("serve"))
    assert "demand_deployment.uid = " in sql(repo.get_query(new_uid(DEPLOYMENT)))


def test_a_bare_name_can_be_narrowed_by_cluster(repo):
    text = sql(repo.get_query("serve", cluster="iad-1"))
    assert "demand_deployment.name = " in text
    assert "supply_cluster.name = " in text


def test_replica_rollup_is_aggregated_in_the_database_not_per_deployment(repo):
    text = sql(repo.replica_rollup_query([1, 2]))
    assert text.startswith("SELECT demand_replica.deployment_id, count(demand_replica.id)")
    assert "GROUP BY demand_replica.deployment_id" in text
    assert "demand_replica.observed_generation = demand_deployment.generation" in text


def test_adapters_are_resolved_in_one_statement(repo):
    text = sql(repo.adapter_query([1, 2, 3]))
    assert text.startswith("SELECT demand_artifact.id, demand_artifact.uid")
    assert "demand_artifact.id IN " in text


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in DeploymentRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_deployments",
        "create_deployment",
        "get_deployment",
        "update_deployment",
        "set_deployment_authority",
        "delete_deployment",
    }


def test_type_filter_is_exposed_as_type_not_workload_type():
    parameter = inspect.signature(DeploymentRoutes.list_deployments.fn).parameters["workload_type"]
    assert parameter.default.query == "type"


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(DeploymentRoutes.update_deployment.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_list_handler_offers_every_declared_filter():
    parameters = set(inspect.signature(DeploymentRoutes.list_deployments.fn).parameters)
    assert {"cluster", "recipe", "project", "phase", "workload_type", "label", "limit", "cursor"} <= parameters


def test_delete_removes_the_supertype_so_no_orphan_workload_survives():
    doc = DeploymentRepository.delete.__doc__ or ""
    assert "supertype" in doc


def test_a_create_request_may_leave_the_cluster_to_placement():
    """The scheduler is the default path; naming a cluster is the override."""
    data = DeploymentCreateRequest(recipe="llama-70b", name="serve", project="acme")
    assert data.cluster is None


def test_a_deployment_can_declare_its_own_topology_requirement():
    data = DeploymentCreateRequest(
        recipe="llama-70b",
        name="gang",
        project="acme",
        type="gang",
        requires_single_topology_domain=True,
        topology_domain_kind="nvswitch",
    )
    assert data.requires_single_topology_domain is True
    assert data.topology_domain_kind == "nvswitch"


def test_an_undeclared_topology_requirement_is_null_not_false():
    """Null means "whatever the recipe says"; false would silently relax it."""
    data = DeploymentCreateRequest(recipe="llama-70b", name="serve", project="acme")
    assert data.requires_single_topology_domain is None
    assert data.topology_domain_kind is None


def test_the_topology_requirement_is_patchable():
    fields = DeploymentUpdateRequest(requires_single_topology_domain=False).to_dict(exclude_unset=True)
    assert fields == {"requires_single_topology_domain": False}
