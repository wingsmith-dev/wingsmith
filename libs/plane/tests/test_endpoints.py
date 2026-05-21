import inspect
import datetime as dt

import pytest

from app.core.ids import ENDPOINT
from app.core.ids import new_uid
from app.db.models import Endpoint
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.modules.endpoints.routes import EndpointRoutes
from app.modules.endpoints.schemas import EndpointSchema
from app.modules.endpoints.schemas import EndpointTargetSchema
from app.modules.endpoints.schemas import EndpointCreateRequest
from app.modules.endpoints.schemas import EndpointTargetRequest
from app.modules.endpoints.schemas import EndpointUpdateRequest
from app.modules.endpoints.services import ROLES
from app.modules.endpoints.services import STRATEGIES
from app.modules.endpoints.services import VISIBILITIES
from app.modules.endpoints.services import SERVING_PHASES
from app.modules.endpoints.services import EndpointService
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
    assert set(EndpointTargetSchema.__struct_fields__) == {
        "deployment_id",
        "weight",
        "role",
        "enabled",
        "served_model_name",
    }


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


# --- the resolution, against a repository that is not a database --------------


class FakeRepo:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    async def resolved(self, ref=None):
        return self.rows


class FakeEndpoint:
    id = 1

    def __init__(self, uid="end_1", name="chat", enabled=True) -> None:
        self.uid = uid
        self.name = name
        self.enabled = enabled
        self.visibility = "public"
        self.routing_strategy = "weighted"
        self.failover_order = []
        self.created_at = EPOCH
        self.updated_at = None


class FakeTarget:
    def __init__(self, weight=100, role="primary", enabled=True, served_model_name=None) -> None:
        self.weight = weight
        self.role = role
        self.enabled = enabled
        self.served_model_name = served_model_name
        self.created_at = EPOCH
        self.updated_at = None


class FakeDeployment:
    def __init__(self, uid="dep_1", name="serve-a", enabled=True) -> None:
        self.id = uid
        self.uid = uid
        self.name = name
        self.enabled = enabled
        self.admission_phase = "placed"
        self.created_at = EPOCH
        self.updated_at = None


class FakeCluster:
    def __init__(self, phase="active", uid="clu_1", name="iad-1", updated_at=None) -> None:
        self.uid = uid
        self.name = name
        self.phase = phase
        self.created_at = EPOCH
        self.updated_at = updated_at


class FakeServing:
    def __init__(self, url="https://iad-1.example", enabled=True) -> None:
        self.url = url
        self.enabled = enabled
        self.credential_id = None
        self.created_at = EPOCH
        self.updated_at = None


class FakeRecipe:
    """Named the way the console names a derived one: for the fit, not the model."""

    def __init__(self, name="llama-3-3-70b-instruct-fp8-tp2-32k", engine_args=None, served_model_name=None) -> None:
        self.name = name
        self.engine_args = engine_args or {}
        self.served_model_name = served_model_name
        self.created_at = EPOCH
        self.updated_at = None


class FakeWorkload:
    def __init__(self, served_model_name=None) -> None:
        self.served_model_name = served_model_name


class FakeArtifact:
    def __init__(self, name="meta-llama/Llama-3.3-70B-Instruct", digest="sha256:beef") -> None:
        self.name = name
        self.digest = digest
        self.created_at = EPOCH
        self.updated_at = None


class FakeRow(tuple):
    """A `Row` is a tuple with labels; the resolution reads it positionally."""

    def __new__(
        cls, endpoint, target, deployment, cluster, serving, quota=None, recipe=None, artifact=None, workload=None
    ):
        return super().__new__(
            cls,
            (
                endpoint,
                target,
                deployment,
                cluster,
                serving,
                quota,
                recipe if recipe is not None else FakeRecipe(),
                artifact if artifact is not None else FakeArtifact(),
                workload if workload is not None else FakeWorkload(),
            ),
        )


def resolution(rows) -> EndpointService:
    svc = EndpointService.__new__(EndpointService)
    svc.db = None
    svc.repo = FakeRepo(rows)
    return svc


def upstream_row(phase="active", endpoint=None, **kwargs) -> FakeRow:
    return FakeRow(
        endpoint or FakeEndpoint(),
        FakeTarget(),
        FakeDeployment(**kwargs),
        FakeCluster(phase=phase),
        FakeServing(),
    )


def tunnelled_row(phase="active", endpoint=None, recipe=None, workload=None, **kwargs) -> FakeRow:
    """A cluster that publishes no serving address, which is every cluster we run."""
    return FakeRow(
        endpoint or FakeEndpoint(),
        FakeTarget(),
        FakeDeployment(**kwargs),
        FakeCluster(phase=phase),
        None,
        recipe=recipe,
        workload=workload,
    )


@pytest.mark.asyncio
async def test_an_active_cluster_with_a_serving_address_is_an_upstream():
    table = await resolution([upstream_row()]).resolved()
    assert [u.cluster_name for u in table.endpoints[0].upstreams] == ["iad-1"]


@pytest.mark.asyncio
async def test_an_upstream_carries_the_name_the_engine_answers_to():
    """Not the endpoint's name: the frontend serves under the artifact's slug.

    A client calling the endpoint by name asks the frontend for a model it has
    never heard of. The gateway rewrites `model` to this, and cannot without being
    told it. The slug rather than the recipe's derived fit name, so that re-fitting
    the same weights does not rename the model every caller codes against.
    """
    table = await resolution([tunnelled_row()]).resolved()
    upstream = table.endpoints[0].upstreams[0]
    assert upstream.served_model_name == "meta-llama/llama-3.3-70b-instruct"
    assert upstream.served_model_name != table.endpoints[0].name


@pytest.mark.asyncio
async def test_a_workloads_declared_name_beats_the_recipes():
    """The workload is the object a person creates; a recipe is shared by many."""
    row = tunnelled_row(
        recipe=FakeRecipe(served_model_name="acme/llama-70b"),
        workload=FakeWorkload(served_model_name="acme/llama-70b-canary"),
    )
    table = await resolution([row]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "acme/llama-70b-canary"


@pytest.mark.asyncio
async def test_a_recipes_declared_name_beats_the_derived_slug():
    row = tunnelled_row(recipe=FakeRecipe(served_model_name="acme/llama-70b"))
    table = await resolution([row]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "acme/llama-70b"


@pytest.mark.asyncio
async def test_an_explicit_served_model_name_wins_over_the_recipe_name():
    """`engine_args` is the operator saying so, and it renders onto the engine."""
    recipe = FakeRecipe(engine_args={"served_model_name": "llama-canary"})
    table = await resolution([tunnelled_row(recipe=recipe)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "llama-canary"


@pytest.mark.asyncio
async def test_two_clusters_running_different_recipes_carry_their_own_served_names():
    """Why this rides the upstream and not the endpoint: there is no single answer."""
    endpoint = FakeEndpoint()
    rows = [
        tunnelled_row(endpoint=endpoint, recipe=FakeRecipe(served_model_name="acme/llama-fp8")),
        tunnelled_row(endpoint=endpoint, recipe=FakeRecipe(served_model_name="acme/llama-bf16")),
    ]
    table = await resolution(rows).resolved()
    served = [u.served_model_name for u in table.endpoints[0].upstreams]
    assert served == ["acme/llama-fp8", "acme/llama-bf16"]


@pytest.mark.asyncio
async def test_renaming_a_recipes_served_model_moves_the_revision_the_gateway_polls():
    """Otherwise a gateway behind a 304 keeps rewriting to a name that is gone."""
    before = await resolution([tunnelled_row()]).resolved()
    renamed = tunnelled_row()
    renamed[6].served_model_name = "acme/llama-70b"
    renamed[6].updated_at = EPOCH + dt.timedelta(hours=1)
    after = await resolution([renamed]).resolved()
    assert after.revision != before.revision
    assert after.endpoints[0].upstreams[0].served_model_name == "acme/llama-70b"


@pytest.mark.asyncio
async def test_a_cluster_with_no_serving_address_is_an_upstream_with_no_url():
    """The tunnelled case, and the only one in production: the cluster id is the route.

    Dropping it would empty the table on every cluster we run — nothing declares a
    `ClusterServing`, so the gateway would be told no model is serving anywhere.
    """
    table = await resolution([tunnelled_row()]).resolved()
    upstream = table.endpoints[0].upstreams[0]
    assert (upstream.url, upstream.credential_id) == ("", None)
    assert upstream.cluster_id == "clu_1"


@pytest.mark.asyncio
async def test_a_tunnelled_cluster_that_is_not_active_contributes_no_upstream():
    """The phase rule holds without an address to hang it on."""
    table = await resolution([tunnelled_row(phase="draining")]).resolved()
    assert table.endpoints[0].upstreams == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pending", "draining", "disabled"])
async def test_a_cluster_that_is_not_active_contributes_no_upstream(phase):
    """`phase` is declared intent, and traffic is the one thing that must obey it."""
    table = await resolution([upstream_row(phase=phase)]).resolved()
    assert table.endpoints[0].upstreams == []


@pytest.mark.asyncio
async def test_a_drained_cluster_is_dropped_while_its_active_neighbour_stays():
    endpoint = FakeEndpoint()
    rows = [
        upstream_row(phase="draining", endpoint=endpoint, uid="dep_a", name="serve-a"),
        upstream_row(phase="active", endpoint=endpoint, uid="dep_b", name="serve-b"),
    ]
    table = await resolution(rows).resolved()
    assert [u.deployment_name for u in table.endpoints[0].upstreams] == ["serve-b"]


@pytest.mark.asyncio
async def test_an_endpoint_whose_every_cluster_is_draining_resolves_empty_and_is_not_omitted():
    """`API.md`: "no such model" and "this model has nowhere to go" differ."""
    endpoint = FakeEndpoint()
    rows = [
        upstream_row(phase="draining", endpoint=endpoint, uid="dep_a"),
        upstream_row(phase="disabled", endpoint=endpoint, uid="dep_b"),
    ]
    table = await resolution(rows).resolved()
    assert [e.name for e in table.endpoints] == ["chat"]
    assert table.endpoints[0].upstreams == []


@pytest.mark.asyncio
async def test_resolving_one_drained_endpoint_by_name_is_not_a_404():
    table = await resolution([upstream_row(phase="draining")]).resolved("chat")
    assert len(table.endpoints) == 1
    assert table.endpoints[0].upstreams == []


@pytest.mark.asyncio
async def test_an_endpoint_that_exists_nowhere_is_still_a_404():
    with pytest.raises(NotFound):
        await resolution([]).resolved("chat")


def test_the_phase_is_filtered_above_the_query_so_the_row_survives_the_drain():
    """A `WHERE` on the cluster would take the endpoint's last row with it."""
    text = sql(EndpointRepository(db=None).resolved_query())
    assert "supply_cluster.phase =" not in text
    assert "supply_cluster.phase IN" not in text
    assert "LEFT OUTER JOIN supply_cluster" in text


@pytest.mark.asyncio
async def test_draining_a_cluster_moves_the_revision_the_gateway_polls():
    """Otherwise a `304` keeps the drained upstream alive in the gateway's table."""
    before = await resolution([upstream_row()]).resolved()
    drained = upstream_row()
    drained[3].phase = "draining"
    drained[3].updated_at = EPOCH + dt.timedelta(hours=1)
    after = await resolution([drained]).resolved()
    assert after.revision != before.revision
    assert after.endpoints[0].revision != before.endpoints[0].revision


def test_only_an_active_cluster_serves():
    assert SERVING_PHASES == {"active"}


@pytest.mark.asyncio
async def test_the_hyphenated_flag_is_what_overrides_the_served_name():
    """`engine_args` holds flag names, and the operator renders `--` + the key verbatim.

    Reading only the underscore form meant a correctly-spelled override was
    invisible to the plane while being the name the engine actually answered to.
    """
    recipe = FakeRecipe(engine_args={"served-model-name": "llama-canary"})
    table = await resolution([tunnelled_row(recipe=recipe)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "llama-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["--served-model-name", "served-model-name"])
async def test_the_hatch_is_read_with_or_without_the_leading_dashes(key):
    """A real recipe holds the dashes, because that is what a command line looks like.

    This is the spelling every recipe in the fleet actually carried, and matching
    the raw key missed all of them. The miss was silent — the plane fell through to
    the recipe's declared name and published it — so the first symptom was Kimi K3
    404ing from inside the cluster with a healthy deployment and an open tunnel.
    """
    recipe = FakeRecipe(engine_args={key: "moonshotai/kimi-k3"})
    table = await resolution([tunnelled_row(recipe=recipe)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "moonshotai/kimi-k3"
