import uuid
import inspect
import datetime as dt

from types import SimpleNamespace

import pytest
import msgspec

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
from app.modules.endpoints.services import check_surface
from app.modules.endpoints.services import check_targets
from app.modules.endpoints.services import endpoint_policy
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


def test_the_public_id_is_patchable_and_the_handle_is_not():
    """An endpoint has two names and only one of them may move.

    `name` is the handle — URLs, metric labels, every link the console holds — and it is absent
    from the update request by construction. `slug` is what a client sends, and correcting it is
    the whole reason the two are separate: it costs one gateway poll and touches no cluster.

    There used to be a third, `display_name`, and a fourth reported on the way out. Four spellings
    of one question is what this asserts is gone.
    """
    assert EndpointUpdateRequest(slug="acme/llama-70b").to_dict(exclude_unset=True) == {"slug": "acme/llama-70b"}
    assert EndpointUpdateRequest().to_dict(exclude_unset=True) == {}
    assert "name" not in EndpointUpdateRequest.__struct_fields__
    assert "display_name" not in EndpointUpdateRequest.__struct_fields__
    assert set(EndpointSchema.__struct_fields__) >= {"name", "slug"}
    assert "model_slug" not in EndpointSchema.__struct_fields__
    assert "resolved_model_slug" not in EndpointSchema.__struct_fields__
    assert "display_name" not in EndpointSchema.__struct_fields__


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


def test_artifact_filter_walks_targets_to_deployments_to_workloads_as_a_subquery(repo):
    """The artifact is the workload's; a deployment is that workload on one cluster.

    One hop shorter than it was. The walk used to end at the recipe, because the
    recipe held the artifact; the workload holds it now, so there is nothing left
    between the deployment and the weights it serves.
    """
    text = sql(repo.list_query(limit=10, artifact="llama-70b"))
    assert "demand_endpoint.id IN (SELECT demand_endpoint_target.endpoint_id" in text
    assert "JOIN demand_deployment ON demand_endpoint_target.deployment_id = demand_deployment.id" in text
    assert "JOIN demand_workload ON demand_deployment.workload_id = demand_workload.id" in text
    assert "demand_workload.artifact_id IN" in text
    assert "demand_recipe" not in text
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

    def __init__(self, uid="end_1", name="chat", enabled=True, slug="", surface=None) -> None:
        self.uid = uid
        self.name = name
        # Defaulted to the name so that a fake built without an opinion behaves like a real
        # endpoint created without one: a slug is never empty on a row, because the create path
        # falls back to the artifact and then to the name before it writes.
        self.slug = slug or name
        self.enabled = enabled
        self.visibility = "public"
        self.routing_strategy = "weighted"
        self.failover_order = []
        self.surface = surface or {}
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


DIGEST = "svg_" + "a" * 64


class FakeWorkload:
    """Named the way the console names a derived fit: for the shape, not the model.

    It carries what a `FakeRecipe` used to as well as what a workload already did.
    The serving configuration moved onto the workload, so the `engine_args` hatch,
    the declared name and the digest the revision is built from are one object's
    fields now rather than two objects' — and the fit-shaped name is still here,
    because it is the string these tests exist to keep off the routing table.
    """

    def __init__(
        self,
        name="llama-3-3-70b-instruct-fp8-tp2-32k",
        engine_args=None,
        served_model_name=None,
        serving_digest=DIGEST,
    ) -> None:
        self.name = name
        self.engine_args = engine_args or {}
        self.served_model_name = served_model_name
        self.serving_digest = serving_digest
        self.created_at = EPOCH
        self.updated_at = None


class FakeArtifact:
    def __init__(
        self,
        name="meta-llama/Llama-3.3-70B-Instruct",
        digest="sha256:beef",
        slug="meta-llama/llama-3.3-70b-instruct",
    ) -> None:
        self.name = name
        self.digest = digest
        self.slug = slug
        self.created_at = EPOCH
        self.updated_at = None


class FakeRow(tuple):
    """A `Row` is a tuple with labels; the resolution reads it positionally."""

    def __new__(cls, endpoint, target, deployment, cluster, serving, quota=None, artifact=None, workload=None):
        return super().__new__(
            cls,
            (
                endpoint,
                target,
                deployment,
                cluster,
                serving,
                quota,
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


def tunnelled_row(phase="active", endpoint=None, workload=None, artifact=None, **kwargs) -> FakeRow:
    """A cluster that publishes no serving address, which is every cluster we run."""
    return FakeRow(
        endpoint or FakeEndpoint(),
        FakeTarget(),
        FakeDeployment(**kwargs),
        FakeCluster(phase=phase),
        None,
        artifact=artifact,
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
    told it. The slug rather than the workload's derived fit name, so that
    re-fitting the same weights does not rename the model every caller codes
    against.
    """
    table = await resolution([tunnelled_row()]).resolved()
    upstream = table.endpoints[0].upstreams[0]
    assert upstream.served_model_name == "meta-llama/llama-3.3-70b-instruct"
    assert upstream.served_model_name != table.endpoints[0].name


@pytest.mark.asyncio
async def test_the_engines_name_is_the_artifacts_slug_and_the_workload_cannot_overrule_it():
    """There used to be a ladder here: the workload's declaration, then the recipe's, then a slug
    the plane re-derived at read time. It is one column now, on the artifact.

    The ladder is what broke the fleet. The plane's derivation could not produce an organisation —
    it read the artifact's bare `name` — while the operator started every engine on a `--model`
    taken from the artifact's location, which had one. The gateway rewrote to the plane's answer
    and the frontend had registered the operator's, so a healthy deployment behind an open tunnel
    answered `model_not_found` from inside the cluster.

    A workload declaring a different name is not an override any more, because there is nothing
    left to override: the string is a fact about the weights.
    """
    row = tunnelled_row(
        workload=FakeWorkload(served_model_name="acme/llama-70b"),
        artifact=FakeArtifact(slug="meta-llama/llama-3.3-70b-instruct"),
    )
    table = await resolution([row]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "meta-llama/llama-3.3-70b-instruct"


@pytest.mark.asyncio
async def test_an_explicit_served_model_name_wins_over_the_declared_name():
    """`engine_args` is the operator saying so, and it renders onto the engine."""
    workload = FakeWorkload(engine_args={"served_model_name": "llama-canary"})
    table = await resolution([tunnelled_row(workload=workload)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "llama-canary"


@pytest.mark.asyncio
async def test_two_clusters_serving_different_weights_carry_their_own_served_names():
    """Why this rides the upstream and not the endpoint: there is no single answer.

    One public name over two deployments of *different* artifacts — a quantised build beside a
    full-precision one — is the canary case, and each engine answers to its own weights' slug.
    That is why the routing table carries the name per upstream: an endpoint-level field would
    have to pick one and rewrite half the traffic to a string nothing had registered.
    """
    endpoint = FakeEndpoint()
    rows = [
        tunnelled_row(endpoint=endpoint, artifact=FakeArtifact(slug="acme/llama-fp8")),
        tunnelled_row(endpoint=endpoint, artifact=FakeArtifact(slug="acme/llama-bf16")),
    ]
    table = await resolution(rows).resolved()
    served = [u.served_model_name for u in table.endpoints[0].upstreams]
    assert served == ["acme/llama-fp8", "acme/llama-bf16"]


@pytest.mark.asyncio
async def test_renaming_the_served_model_moves_the_revision_the_gateway_polls():
    """Otherwise a gateway behind a 304 keeps rewriting to a name that is gone.

    Nothing is re-stamped here, and that is the assertion. The revision used to take a row's
    `updated_at`, which moved for edits no engine ever sees and did not move for the rename
    itself. The third term is content now: the serving digests and the resolved names themselves.

    The rename is on the artifact, because that is where the name lives. A slug corrected on an
    artifact that three clusters serve has to reach every gateway on the next poll, and the row
    the gateway would otherwise watch — the endpoint's — has not been touched at all.
    """
    before = await resolution([tunnelled_row()]).resolved()
    renamed = tunnelled_row(artifact=FakeArtifact(slug="acme/llama-70b"))
    assert renamed[6].updated_at is None
    after = await resolution([renamed]).resolved()
    assert after.revision != before.revision
    assert after.endpoints[0].upstreams[0].served_model_name == "acme/llama-70b"


@pytest.mark.asyncio
async def test_a_configuration_change_alone_moves_the_revision():
    """The other half of the same term: the digest is what a re-fit changes.

    A workload re-fitted at a different tensor degree serves the same model under
    the same name, so the resolved names say nothing happened. The digest does,
    and a gateway sitting behind a 304 would otherwise never re-read the table.
    """
    before = await resolution([tunnelled_row()]).resolved()
    refitted = tunnelled_row(workload=FakeWorkload(serving_digest="svg_" + "b" * 64))
    after = await resolution([refitted]).resolved()
    assert after.revision != before.revision


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
    workload = FakeWorkload(engine_args={"served-model-name": "llama-canary"})
    table = await resolution([tunnelled_row(workload=workload)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "llama-canary"


@pytest.mark.asyncio
async def test_the_resolved_name_is_the_slug_because_that_is_what_the_gateway_routes_on():
    """`model` is a public contract; the endpoint's name is an internal handle.

    The gateway builds its route map on whatever the resolved table calls the
    endpoint, so reporting the slug here is the entire mechanism by which a public
    model id can be corrected without an engine flag or a pod respec.
    """
    endpoint = FakeEndpoint(name="kimi-k3-overhead-test", slug="moonshotai/kimi-k3")
    table = await resolution([tunnelled_row(endpoint=endpoint)]).resolved()
    assert table.endpoints[0].name == "moonshotai/kimi-k3"


class _CreatedRow(tuple):
    """What `repo.get` hands back: the endpoint, labelled with its project."""

    @property
    def project_uid(self):
        return "prj_1"


class FakeCreateRepo:
    """Enough of the repository for `create` to run with no database under it."""

    def __init__(self, artifact_slug="", deployment=None) -> None:
        self.artifact_slug_value = artifact_slug
        self.deployment = deployment
        self.added = None

    async def project_by_ref(self, ref):
        return SimpleNamespace(id=uuid.uuid4(), uid="prj_1")

    async def deployments_by_ref(self, refs):
        if self.deployment is None:
            return {}
        return {ref: self.deployment for ref in refs}

    async def artifact_slug(self, deployment_id):
        return self.artifact_slug_value

    async def add(self, endpoint):
        endpoint.created_at = EPOCH
        self.added = endpoint
        return endpoint

    async def get(self, ref):
        return _CreatedRow((self.added,))

    async def deployment_uids(self, ids):
        return {}


def creating(repo) -> EndpointService:
    svc = EndpointService.__new__(EndpointService)
    svc.db = SimpleNamespace(commit=_noop, rollback=_noop, flush=_noop)
    svc.repo = repo
    return svc


async def _noop():
    return None


@pytest.mark.asyncio
async def test_an_endpoint_that_declares_no_slug_takes_the_name_of_the_weights_behind_it():
    """The default is the point of the field, not a convenience.

    An endpoint in front of one model answers to that model's name, and making a
    caller type it a second time is exactly how the public id and the id the
    engine registered come to differ — which is not a visible failure but a 404
    from inside the cluster with the deployment healthy and the tunnel open.
    """
    repo = FakeCreateRepo(artifact_slug="moonshotai/kimi-k3", deployment=SimpleNamespace(id=uuid.uuid4()))
    created = await creating(repo).create(
        EndpointCreateRequest(name="kimi-k3-overhead-test", project="acme", targets=[target()])
    )
    assert created.slug == "moonshotai/kimi-k3"


@pytest.mark.asyncio
async def test_an_endpoint_with_nothing_to_derive_a_slug_from_still_routes_on_its_name():
    """The floor, and why the column is never null: a route needs a key.

    An endpoint with no targets yet has no weights to read a name off, and an
    empty slug would publish a route on the empty string.
    """
    created = await creating(FakeCreateRepo()).create(EndpointCreateRequest(name="chat", project="acme"))
    assert created.slug == "chat"


@pytest.mark.asyncio
async def test_a_declared_slug_is_taken_as_an_alias_and_lowercased():
    repo = FakeCreateRepo(artifact_slug="moonshotai/kimi-k3", deployment=SimpleNamespace(id=uuid.uuid4()))
    created = await creating(repo).create(
        EndpointCreateRequest(name="chat", project="acme", slug="Acme/Chat", targets=[target()])
    )
    assert created.slug == "acme/chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["--served-model-name", "served-model-name"])
async def test_the_hatch_is_read_with_or_without_the_leading_dashes(key):
    """A real workload holds the dashes, because that is what a command line looks like.

    This is the spelling every serving configuration in the fleet actually carried,
    and matching the raw key missed all of them. The miss was silent — the plane
    fell through to the declared name and published it — so the first symptom was
    Kimi K3 404ing from inside the cluster with a healthy deployment and an open
    tunnel.
    """
    workload = FakeWorkload(engine_args={key: "moonshotai/kimi-k3"})
    table = await resolution([tunnelled_row(workload=workload)]).resolved()
    assert table.endpoints[0].upstreams[0].served_model_name == "moonshotai/kimi-k3"


KIMI_POLICY_V1 = {
    "policy": {
        "parameters": {
            "temperature": {"enum": [1]},
            "top_p": {"enum": [0.95]},
            "presence_penalty": {"enum": [0]},
            "frequency_penalty": {"enum": [0]},
            "logprobs": {"enum": [False]},
        },
        "deny": ["tool_choice.named_while_thinking"],
    }
}
"""What Moonshot answered 400 to on 2026-09-01, in the language it was written in.

Kept as a fixture because it is what is actually stored. Nothing writes this
shape any more, and everything still has to read it."""


CHAT = "/v1/chat/completions"

KIMI_POLICY = {
    "policy": {
        "version": 2,
        "rules": [
            {"route": CHAT, "key": "temperature", "enum": [1]},
            {"route": CHAT, "key": "top_p", "enum": [0.95]},
            {"route": CHAT, "key": "presence_penalty", "enum": [0]},
            {"route": CHAT, "key": "frequency_penalty", "enum": [0]},
            {"route": CHAT, "key": "logprobs", "enum": [False]},
            {"route": CHAT, "predicate": "tool_choice.named_while_thinking", "deny": True},
            {"route": CHAT, "key": "thinking.type", "default": "enabled"},
            {"route": CHAT, "when_present": "thinking.effort", "key": "reasoning_effort", "drop": True},
        ],
    }
}
"""The same terms as rules, plus the two v1 could not say.

Every pin carries the path it was measured on, so none of them reaches
`/v1/embeddings`, `/v1/messages` or `/v1/responses`.

The `thinking.type` default is Moonshot's contract that an omitted type means
enabled, which the frontend behind this endpoint answers 400 to instead. The
`reasoning_effort` drop is their contract that `thinking.effort` beats the
OpenAI-style field and the loser is ignored rather than refused — measured on our
endpoint as a reasoning trace that did not get shorter when it should have."""


@pytest.mark.asyncio
async def test_an_endpoint_that_sold_no_terms_resolves_with_no_policy():
    """The default, and the reason this is per endpoint rather than per gateway.

    Conforming on one model must never narrow another, so absence has to reach
    the gateway as null and mean "forward what you were given".
    """
    table = await resolution([tunnelled_row(endpoint=FakeEndpoint())]).resolved()
    assert table.endpoints[0].policy is None


@pytest.mark.asyncio
async def test_a_sold_policy_reaches_the_resolved_table():
    endpoint = FakeEndpoint(name="moonshotai/kimi-k3", surface=KIMI_POLICY)
    table = await resolution([tunnelled_row(endpoint=endpoint)]).resolved()
    policy = table.endpoints[0].policy
    assert policy is not None
    assert policy.version == 2

    by_key = {rule.key: rule for rule in policy.rules if rule.key}
    assert by_key["temperature"].enum == [1]
    assert by_key["logprobs"].enum == [False]
    assert by_key["thinking.type"].default == "enabled"
    assert by_key["reasoning_effort"].drop is True
    assert by_key["reasoning_effort"].when_present == "thinking.effort"
    assert all(rule.route == CHAT for rule in policy.rules)
    assert [rule.predicate for rule in policy.rules if rule.predicate] == ["tool_choice.named_while_thinking"]


@pytest.mark.asyncio
async def test_a_policy_stored_in_the_old_shape_reaches_the_gateway_as_rules():
    """The migration, and it is a read rather than a batch job.

    Converting here is what makes it one nobody has to schedule: no window where
    half the rows are in one language, and no way for a policy written before
    this change to reach a gateway in a shape it stopped reading.
    """
    endpoint = FakeEndpoint(name="moonshotai/kimi-k3", surface=KIMI_POLICY_V1)
    table = await resolution([tunnelled_row(endpoint=endpoint)]).resolved()
    policy = table.endpoints[0].policy
    assert policy is not None
    assert policy.version == 2

    by_key = {rule.key: rule for rule in policy.rules if rule.key}
    assert by_key["temperature"].enum == [1]
    assert by_key["logprobs"].enum == [False]

    denied = [rule for rule in policy.rules if rule.predicate]
    assert [rule.predicate for rule in denied] == ["tool_choice.named_while_thinking"]
    assert all(rule.deny for rule in denied)


def test_a_rule_that_only_removes_a_field_is_valid():
    """`drop` is the term for a field the provider accepts and ignores.

    Denying one of those would 400 a request the vendor serves, and leaving it in
    place lets an engine honour a field the provider does not — a wrong answer
    rather than an error, and the harder of the two to notice.
    """
    check_surface(
        {
            "policy": {
                "rules": [
                    {"when_present": "thinking.effort", "key": "reasoning_effort", "drop": True},
                    {"key": "logit_bias", "drop": True},
                ]
            }
        }
    )


def test_two_drops_of_one_key_on_different_conditions_are_accepted():
    """Not a disagreement: both take the field out, in either order, and two
    reasons to remove one field is an ordinary thing to want."""
    check_surface(
        {
            "policy": {
                "rules": [
                    {"when_present": "thinking.effort", "key": "reasoning_effort", "drop": True},
                    {"when_present": "thinking.type", "key": "reasoning_effort", "drop": True},
                ]
            }
        }
    )


def test_a_default_and_a_drop_on_one_key_are_refused():
    """One puts the field in and the other takes it out, on requests that match
    both. There is no answer to that on the request path, so it is refused here."""
    with pytest.raises(Invalid):
        check_surface(
            {
                "policy": {
                    "rules": [
                        {"key": "reasoning_effort", "default": "max"},
                        {"when_present": "thinking.effort", "key": "reasoning_effort", "drop": True},
                    ]
                }
            }
        )


def test_two_writes_guarded_on_different_paths_still_conflict():
    """`when_present` narrows nothing for this question: a body carrying both
    conditions matches both rules, which is exactly the conflict being refused."""
    with pytest.raises(Invalid):
        check_surface(
            {
                "policy": {
                    "rules": [
                        {"when_present": "a", "key": "k", "default": 1},
                        {"when_present": "b", "key": "k", "default": 2},
                    ]
                }
            }
        )


def test_a_migrated_policy_keeps_the_scope_it_had_which_is_none():
    """v1 had no route, so a term it sold applied to every path — and it still does.

    Narrowing these to chat completions would be the right guess for every policy
    in the fleet today and still the wrong thing to do: a migration that quietly
    stops enforcing a sold term on two protocols is the failure this object
    exists to prevent. Scoping is an edit an operator makes, one endpoint at a
    time, and it is visible in the surface when they do.
    """
    policy = endpoint_policy(KIMI_POLICY_V1)
    assert policy is not None
    assert all(rule.route == "" for rule in policy.rules)
    assert all(rule.stream is None for rule in policy.rules)


def test_a_converted_policy_is_the_same_bytes_every_time():
    """The gateway polls with `If-None-Match`. A conversion that reordered itself
    between two reads would move the revision on a table nothing had changed in."""
    first = msgspec.json.encode(endpoint_policy(KIMI_POLICY_V1))
    for _ in range(5):
        assert msgspec.json.encode(endpoint_policy(KIMI_POLICY_V1)) == first


def test_a_rule_that_constrains_nothing_is_refused():
    """An empty rule would read as a pin and enforce nothing, which is the one
    failure mode a conformance policy must not have: silently not conforming."""
    with pytest.raises(Invalid):
        check_surface({"policy": {"parameters": {"temperature": {}}}})
    with pytest.raises(Invalid):
        check_surface({"policy": {"rules": [{"key": "temperature"}]}})


def test_a_rule_that_only_fills_a_field_in_is_valid():
    """A default constrains nothing and is still a term. It is the half of this
    language that writes instead of refusing, and refusing it as empty would make
    the one rule K3 needs unwritable."""
    check_surface({"policy": {"rules": [{"key": "thinking.type", "default": "enabled"}]}})


def test_an_inverted_range_is_refused():
    with pytest.raises(Invalid):
        check_surface({"policy": {"parameters": {"temperature": {"min": 5, "max": 1}}}})
    with pytest.raises(Invalid):
        check_surface({"policy": {"rules": [{"key": "temperature", "min": 5, "max": 1}]}})


def test_an_unknown_field_is_refused_rather_than_ignored():
    """A typo in a policy is a conformance gap that looks like a working config,
    so it is refused on write rather than dropped on read."""
    with pytest.raises(Invalid):
        check_surface({"policy": {"parmeters": {"temperature": {"enum": [1]}}}})
    with pytest.raises(Invalid):
        check_surface({"policy": {"rules": [{"kye": "temperature", "enum": [1]}]}})


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param({"key": "a", "predicate": "b", "deny": True}, id="both subjects"),
        pytest.param({"enum": [1]}, id="neither subject"),
        pytest.param({"predicate": "x", "deny": True, "enum": [1]}, id="a predicate with a value term"),
        pytest.param({"predicate": "x", "deny": True, "default": 1}, id="a predicate with a default"),
        pytest.param({"predicate": "x"}, id="a predicate that denies nothing"),
        pytest.param({"key": "a", "deny": True, "enum": [1]}, id="a denied key with an unreachable enum"),
        pytest.param({"route": "v1/chat/completions", "key": "a", "enum": [1]}, id="a route that is not a path"),
        pytest.param({"key": "thinking..type", "enum": [1]}, id="a key with an empty segment"),
        pytest.param({"key": ".thinking", "enum": [1]}, id="a key with a leading dot"),
        pytest.param({"predicate": "x", "deny": True, "drop": True}, id="a predicate with nothing to drop"),
        pytest.param({"key": "a", "deny": True, "drop": True}, id="a key both refused and removed"),
        pytest.param({"key": "a", "drop": True, "default": 1}, id="a key both removed and filled in"),
        pytest.param({"key": "a", "drop": True, "when_present": "a"}, id="a condition on the rule's own key"),
        pytest.param({"key": "a", "drop": True, "when_present": "b..c"}, id="a condition with an empty segment"),
    ],
)
def test_a_rule_the_gateway_could_not_act_on_is_refused(rule):
    """Every one of these would be stored, read, and enforce nothing.

    A key and a predicate are different namespaces — one is read out of the body,
    the other is looked up in a registry — so a rule that names both names
    nothing the gateway can resolve. A route that is not a path matches no
    request. A key with an empty segment resolves to no value. None of them has a
    symptom anywhere downstream, which is why they have to fail on write.
    """
    with pytest.raises(Invalid):
        check_surface({"policy": {"rules": [rule]}})


def test_two_defaults_for_one_key_on_overlapping_scopes_are_refused():
    """Resolved here so the gateway never has to.

    A default is applied to a request in flight and there is no good answer to
    "which of these two values" at that point: picking by array order would make
    the order semantic, and picking the narrower scope would make a policy mean
    something other than what each of its rules says.
    """
    with pytest.raises(Invalid):
        check_surface(
            {
                "policy": {
                    "rules": [
                        {"key": "thinking.type", "default": "enabled"},
                        {"route": CHAT, "key": "thinking.type", "default": "disabled"},
                    ]
                }
            }
        )


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param(
            [
                {"route": CHAT, "key": "thinking.type", "default": "enabled"},
                {"route": "/v1/messages", "key": "thinking.type", "default": "disabled"},
            ],
            id="different routes",
        ),
        pytest.param(
            [
                {"stream": True, "key": "thinking.type", "default": "enabled"},
                {"stream": False, "key": "thinking.type", "default": "disabled"},
            ],
            id="different stream modes",
        ),
        pytest.param(
            [
                {"key": "thinking.type", "default": "enabled"},
                {"key": "thinking.keep", "default": "all"},
            ],
            id="different keys",
        ),
    ],
)
def test_two_defaults_that_cannot_both_fire_are_accepted(rules):
    check_surface({"policy": {"rules": rules}})


def test_rules_at_version_one_are_refused():
    """The version names the shape. A policy that claims the language it is not
    written in is a policy a reader would enforce the wrong half of."""
    with pytest.raises(Invalid):
        check_surface({"policy": {"version": 1, "rules": [{"key": "a", "enum": [1]}]}})


def test_the_two_languages_cannot_be_mixed_in_one_policy():
    with pytest.raises(Invalid):
        check_surface({"policy": {"parameters": {"a": {"enum": [1]}}, "rules": [{"key": "a", "enum": [1]}]}})


def test_a_surface_without_a_policy_is_valid():
    check_surface({})
    check_surface({"output_length_params": ["max_tokens"]})


@pytest.mark.parametrize(
    "surface",
    [
        {"auth_mode": "nonsense"},
        {"protocols": ["anthropic.messages"]},
        {"streaming_transport": "websocket"},
        {"error_shape": "wrapped"},
        {"cors_enabled": "yes"},
        {"max_request_bytes": -1},
        {"request_timeout_seconds": 0},
        {"output_length_params": ["max_toknes"]},
    ],
)
def test_a_surface_value_the_gateway_could_not_read_is_refused(surface):
    """The surface is patchable and half of it the gateway acts on.

    A bad `streaming_transport` is also a term an operator reads off a page and
    repeats to a customer, so a typo here has to fail on write — nothing
    downstream would catch it.
    """
    with pytest.raises(Invalid):
        check_surface(surface)


def test_a_whole_valid_surface_is_accepted():
    check_surface(
        {
            "protocols": ["openai.chat", "openai.responses"],
            "auth_mode": "api_key",
            "error_shape": "passthrough",
            "streaming_transport": "sse",
            "cors_enabled": True,
            "max_request_bytes": 33554432,
            "request_timeout_seconds": 600,
            "output_length_params": ["max_tokens", "max_completion_tokens"],
            "policy": KIMI_POLICY["policy"],
        }
    )
