import inspect
import datetime as dt

from types import SimpleNamespace

import pytest

from app.core.ids import CLUSTER
from app.core.ids import DEPLOYMENT
from app.core.ids import new_uid
from app.db.models import Deployment
from app.modules.deployments.routes import DeploymentRoutes
from app.modules.deployments.schemas import DeploymentSchema
from app.modules.deployments.schemas import DeploymentUpdateRequest
from app.modules.deployments.schemas import DeploymentReplicaRollupSchema
from app.modules.deployments.services import EMPTY
from app.modules.deployments.services import PHASES
from app.modules.deployments.services import AUTHORITIES
from app.modules.deployments.services import _status
from app.modules.deployments.services import _revision
from app.modules.deployments.repository import DeploymentRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return DeploymentRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_update_request_leaves_absent_fields_unset():
    data = DeploymentUpdateRequest(enabled=False)
    assert data.to_dict(exclude_unset=True) == {"enabled": False}


def test_a_deployment_declares_nothing_the_workload_already_declares():
    """The serving configuration, scale and cluster are the workload's; a share is not patchable."""
    fields = set(DeploymentUpdateRequest.__struct_fields__)
    assert fields == {"enabled"}


def test_admission_phase_is_not_client_assertable():
    assert "phase" not in DeploymentUpdateRequest.__struct_fields__


def test_admission_is_per_cluster_and_lives_on_the_deployment():
    fields = set(DeploymentSchema.__struct_fields__)
    assert {"admission_phase", "admission_reason", "queued_at", "admitted_at", "placed_at"} <= fields


def test_workload_facts_are_named_not_copied():
    """A share carries the parent's identity, not the parent's spec."""
    fields = set(DeploymentSchema.__struct_fields__)
    assert {"workload_id", "workload_name"} <= fields
    assert not fields & {"serving", "serving_digest", "adapter_artifacts", "overrides", "labels", "type", "kind"}
    assert not fields & {"priority_class", "engine", "engine_args", "device_class"}
    assert not fields & {"requires_single_topology_domain", "topology_domain_kind"}


def test_admission_phases_cover_the_whole_lifecycle():
    assert {"pending", "admitted", "placed", "running"} <= PHASES


def test_authority_is_a_two_valued_choice():
    assert AUTHORITIES == {"observed", "managed"}


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


def test_revision_is_the_deployments_own_stamp():
    """No write path here reaches the workload, so one row settles the version."""
    later = Deployment(created_at=EPOCH, updated_at=EPOCH + dt.timedelta(seconds=5))
    assert _revision(later) == str(int((EPOCH + dt.timedelta(seconds=5)).timestamp() * 1_000_000))
    assert _revision(later) != _revision(Deployment(created_at=EPOCH, updated_at=None))


def test_base_query_joins_the_parent_workload_and_the_cluster(repo):
    text = sql(repo.list_query(limit=10))
    assert "FROM demand_deployment JOIN demand_workload ON demand_deployment.workload_id = demand_workload.id" in text
    assert "JOIN supply_cluster" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            cluster="iad-1",
            workload="llama-70b-prod",
            project="acme",
            phase="running",
        )
    )
    assert "demand_deployment.seq > " in text
    assert "supply_cluster.name = " in text
    assert "demand_workload.name = " in text
    assert "bind_project.slug = " in text
    assert "demand_deployment.admission_phase = " in text
    assert "ORDER BY demand_deployment.seq ASC" in text


def test_phase_filters_on_the_deployments_own_admission(repo):
    """Not the workload's: a workload is running here and queued there."""
    text = sql(repo.list_query(limit=10, phase="running"))
    assert "demand_deployment.admission_phase = " in text
    assert "demand_workload.admission_phase" not in text


def test_reference_filters_resolve_an_id_against_uid_not_name(repo):
    text = sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER)))
    assert "supply_cluster.uid = " in text


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


def test_controller_is_read_mostly():
    """Placement creates a deployment and contraction removes it; a caller does neither."""
    handlers = {h.handler_name for h in DeploymentRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_deployments",
        "get_deployment",
        "update_deployment",
        "set_deployment_authority",
    }


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(DeploymentRoutes.update_deployment.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_list_handler_offers_every_declared_filter():
    parameters = set(inspect.signature(DeploymentRoutes.list_deployments.fn).parameters)
    assert {"cluster", "workload", "project", "phase", "limit", "cursor"} <= parameters
    assert not parameters & {"serving_digest", "workload_type", "label"}


def test_the_repository_cannot_create_or_delete():
    """Both are placement's, through the workload; neither has a route to reach here."""
    assert not hasattr(DeploymentRepository, "add")
    assert not hasattr(DeploymentRepository, "delete")


def test_the_per_role_counts_reach_the_api_beside_the_scalars() -> None:
    """A rollout's only readable half, on a deployment that has roles.

    The scalars stay the group rollup and are not replaced, so a client that ignores
    `roles` reads exactly the totals it always did. What it cannot read is which role
    is short, and for a 1P:2D group that is the whole question, because two ready
    decode workers and no prefill is `2 of 3` and serves nothing.
    """
    status = _status(
        _status_row([{"role": "prefill", "desired": 1, "ready": 1}, {"role": "decode", "desired": 2, "ready": 1}])
    )
    assert status is not None
    assert status.ready_replicas == 2 and status.desired_replicas == 3
    assert [(r.role, r.desired, r.ready) for r in status.roles] == [("prefill", 1, 1), ("decode", 2, 1)]


def test_an_aggregated_deployment_reports_no_roles_rather_than_none_of_them() -> None:
    """`None` and `[]` are different claims, and the column stores the second.

    An empty array out of the API would say this deployment's roles are known to be
    zero, where the truth is that the question does not apply to it.
    """
    assert _status(_status_row([])).roles is None


def _status_row(roles: list[dict]):
    """The columns `_status` reads, with only the ones under test carrying anything."""
    return SimpleNamespace(
        observed_generation=14,
        phase="degraded",
        reason="Progressing",
        message="2 of 3 replicas ready",
        desired_replicas=3,
        ready_replicas=2,
        updated_replicas=3,
        unavailable_replicas=1,
        roles=roles,
        last_error_code=None,
        last_error_message=None,
        last_error_at=None,
        observed_at=EPOCH,
    )
