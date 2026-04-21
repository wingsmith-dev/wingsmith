import inspect
import datetime as dt

import pytest

from app.core.ids import ENDPOINT
from app.core.ids import new_uid
from app.db.models import Endpoint
from app.core.exceptions import Invalid
from app.modules.endpoints.routes import EndpointRoutes
from app.modules.endpoints.schemas import EndpointSchema
from app.modules.endpoints.schemas import EndpointTargetSchema
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.endpoints.services import ROLES
from app.modules.endpoints.services import STRATEGIES
from app.modules.endpoints.services import VISIBILITIES
from app.modules.endpoints.services import _revision
from app.modules.endpoints.services import check_shape
from app.modules.endpoints.services import check_targets
from app.modules.endpoints.repository import EndpointRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return EndpointRepository(db=None)


def sql(query) -> str:
    return str(query)


def target(deployment: str = "serve-a", **kwargs) -> EndpointTargetRequest:
    return EndpointTargetRequest(deployment=deployment, **kwargs)


def test_no_schema_in_this_module_mentions_a_cluster():
    """The split is the point: an endpoint is global and never cluster-scoped."""
    for struct in (
        EndpointSchema,
        EndpointTargetSchema,
        EndpointCreateRequest,
        EndpointUpdateRequest,
        EndpointTargetRequest,
    ):
        assert not [f for f in struct.__struct_fields__ if "cluster" in f], struct


def test_no_handler_takes_a_cluster():
    for handler in ("list_endpoints", "get_endpoint", "create_endpoint", "update_endpoint", "delete_endpoint"):
        parameters = inspect.signature(getattr(EndpointRoutes, handler).fn).parameters
        assert "cluster" not in parameters


def test_create_request_defaults_are_a_private_weighted_endpoint():
    data = EndpointCreateRequest(name="chat", project="acme")
    assert data.visibility == "private"
    assert data.routing_strategy == "weighted"
    assert data.enabled is True
    assert data.targets == []
    assert data.failover_order == []


def test_target_defaults_carry_full_weight_as_primary():
    assert (target().weight, target().role, target().enabled) == (100, "primary", True)


def test_update_request_leaves_absent_fields_unset():
    data = EndpointUpdateRequest(enabled=False)
    assert data.to_dict(exclude_unset=True) == {"enabled": False}


def test_the_public_name_and_the_owning_project_are_not_patchable():
    fields = EndpointUpdateRequest.__struct_fields__
    assert "name" not in fields
    assert "project" not in fields


def test_display_name_can_be_cleared_by_null_but_absence_leaves_it_alone():
    assert EndpointUpdateRequest(display_name=None).to_dict(exclude_unset=True) == {"display_name": None}
    assert EndpointUpdateRequest().to_dict(exclude_unset=True) == {}


def test_failover_order_is_an_ordered_list_of_deployments():
    fields = EndpointSchema.__struct_fields__
    assert "failover_order" in fields
    assert EndpointSchema.__annotations__["failover_order"] == list[str]


def test_targets_are_the_weighted_many_to_many():
    assert set(EndpointTargetSchema.__struct_fields__) == {"deployment_id", "weight", "role", "enabled"}


def test_accepted_vocabularies():
    assert VISIBILITIES == {"private", "public"}
    assert "weighted" in STRATEGIES and "failover" in STRATEGIES
    assert "primary" in ROLES and "canary" in ROLES


@pytest.mark.parametrize(
    ("visibility", "strategy"),
    [("public", "weighted"), ("private", "failover")],
)
def test_shape_that_is_accepted(visibility, strategy):
    assert check_shape(visibility, strategy) is None


@pytest.mark.parametrize(
    ("visibility", "strategy"),
    [("secret", "weighted"), ("public", "sticky")],
)
def test_shape_that_is_refused(visibility, strategy):
    with pytest.raises(Invalid):
        check_shape(visibility, strategy)


def test_an_empty_target_set_is_allowed():
    assert check_targets([]) is None


def test_a_deployment_cannot_appear_behind_one_endpoint_twice():
    with pytest.raises(Invalid):
        check_targets([target("serve-a"), target("serve-a", weight=1)])


def test_a_negative_weight_is_refused():
    with pytest.raises(Invalid):
        check_targets([target(weight=-1)])


def test_an_unknown_role_is_refused():
    with pytest.raises(Invalid):
        check_targets([target(role="shadow")])


def test_enabled_targets_that_all_carry_zero_weight_are_refused():
    with pytest.raises(Invalid):
        check_targets([target("serve-a", weight=0), target("serve-b", weight=0)])


def test_a_disabled_target_does_not_count_towards_live_weight():
    assert check_targets([target("serve-a", weight=100), target("serve-b", weight=0, enabled=False)]) is None
    with pytest.raises(Invalid):
        check_targets([target("serve-a", weight=100, enabled=False)])


def test_label_selector_becomes_a_predicate_per_key(repo):
    text = sql(repo.list_query(limit=10, labels={"tier": "gold", "team": "search"}))
    assert text.count("demand_endpoint.labels ->>") == 2
    assert "->>" not in sql(repo.list_query(limit=10))


def test_revision_is_derived_from_the_last_write():
    assert _revision(Endpoint(created_at=EPOCH, updated_at=None)) == str(int(EPOCH.timestamp() * 1_000_000))
    later = Endpoint(created_at=EPOCH, updated_at=EPOCH + dt.timedelta(seconds=1))
    assert _revision(later) != _revision(Endpoint(created_at=EPOCH, updated_at=None))


def test_list_query_never_touches_a_cluster_table(repo):
    text = sql(repo.list_query(limit=10, project="acme", artifact="llama-70b", search="chat"))
    assert "supply_cluster" not in text
    assert "supply_node" not in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(repo.list_query(limit=10, after_seq=41, project="acme", artifact="llama-70b", search="chat"))
    assert "FROM demand_endpoint JOIN bind_project" in text
    assert "demand_endpoint.seq > " in text
    assert "bind_project.slug = " in text
    assert "lower(demand_endpoint.name) LIKE lower(" in text
    assert "ORDER BY demand_endpoint.seq ASC" in text


def test_artifact_filter_walks_targets_to_deployments_to_workloads_to_recipes_as_a_subquery(repo):
    """The recipe is the workload's; a deployment is that workload on one cluster."""
    text = sql(repo.list_query(limit=10, artifact="llama-70b"))
    assert "demand_endpoint.id IN (SELECT demand_endpoint_target.endpoint_id" in text
    assert "JOIN demand_deployment ON demand_endpoint_target.deployment_id = demand_deployment.id" in text
    assert "JOIN demand_workload ON demand_deployment.workload_id = demand_workload.id" in text
    assert "JOIN demand_recipe ON demand_workload.recipe_id = demand_recipe.id" in text
    assert "demand_artifact.name = " in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_endpoint.name = " in sql(repo.get_query("chat"))
    assert "demand_endpoint.uid = " in sql(repo.get_query(new_uid(ENDPOINT)))


def test_target_deployment_uids_are_resolved_in_one_statement(repo):
    text = sql(repo.deployment_uid_query([1, 2]))
    assert text.startswith("SELECT demand_deployment.id, demand_deployment.uid")
    assert "demand_deployment.id IN " in text


def test_controller_exposes_full_crud():
    handlers = {h.handler_name for h in EndpointRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_endpoints",
        "create_endpoint",
        "get_endpoint",
        "update_endpoint",
        "delete_endpoint",
        "resolve_all",
        "resolve_one",
    }


def test_resolution_is_unpaginated_and_conditional():
    """The table is fetched whole, and the steady state of polling it is a 304."""
    parameters = set(inspect.signature(EndpointRoutes.resolve_all.fn).parameters)
    assert "if_none_match" in parameters
    assert not parameters & {"limit", "cursor"}


def test_list_handler_offers_every_declared_filter():
    parameters = set(inspect.signature(EndpointRoutes.list_endpoints.fn).parameters)
    assert {"project", "artifact", "workload", "search", "label", "limit", "cursor"} <= parameters


def test_workload_filter_walks_targets_to_deployments_as_a_reverse_index(repo):
    text = sql(repo.serving_workload_query("wkl-1"))
    assert text.startswith("SELECT demand_endpoint_target.endpoint_id")
    assert "demand_deployment" in text
    assert "demand_workload" in text.lower() or "workload_id" in text


def test_workload_filter_compiles_into_one_statement_as_a_subquery(repo):
    text = sql(repo.list_query(limit=10, workload="wkl-1"))
    assert "demand_endpoint.id IN " in text
    assert "demand_endpoint_target" in text


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(EndpointRoutes.update_endpoint.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"
