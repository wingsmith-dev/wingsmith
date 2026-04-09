import datetime as dt

import pytest
import msgspec

from app.core.ids import CLUSTER
from app.core.ids import DEPLOYMENT
from app.core.ids import BENCHMARK_RUN
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.benchmarks import harnesses
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.benchmarks.schemas import BenchmarkRunSchema
from app.modules.benchmarks.schemas import BenchmarkProvenanceSchema
from app.modules.benchmarks.schemas import BenchmarkRunCreateRequest
from app.modules.benchmarks.services import OPERATION_TYPE
from app.modules.benchmarks.services import BenchmarkService
from app.modules.operations.services import TYPES as OPERATION_TYPES
from app.modules.benchmarks.repository import BenchmarkRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return BenchmarkRepository(db=None)


def sql(query) -> str:
    return str(query)


# ---------------------------------------------------------------- harnesses


def test_defaults_come_from_the_harness_when_nothing_is_supplied():
    resolved = harnesses.normalise(harnesses.AIPERF, {})
    assert resolved["concurrency"] == 10
    assert resolved["request_count"] == 100
    assert resolved["streaming"] is True


def test_a_flag_the_harness_does_not_accept_is_refused_here():
    """A typo must be a 400 at creation, not a job that fails in a cluster ten minutes later."""
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrancy": 10})


def test_an_unknown_harness_is_refused():
    with pytest.raises(Invalid):
        harnesses.resolve("wrk")


def test_form_posted_strings_are_coerced_to_the_declared_type():
    resolved = harnesses.normalise(harnesses.AIPERF, {"concurrency": "32", "streaming": "false"})
    assert resolved["concurrency"] == 32
    assert resolved["streaming"] is False


def test_a_value_that_cannot_be_coerced_is_refused_rather_than_defaulted():
    """Silently defaulting would produce a run that measured something nobody asked for."""
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrency": "lots"})
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrency": -4})


def test_wire_parameters_are_strings_go_can_parse():
    """`strconv.ParseBool` does not read Python's capitalised `True`."""
    flattened = harnesses.wire_parameters(harnesses.AIPERF, harnesses.normalise(harnesses.AIPERF, {}))
    assert flattened["streaming"] == "true"
    assert flattened["harness"] == "aiperf"
    assert flattened["image"] == harnesses.AIPERF.image
    assert all(isinstance(value, str) for value in flattened.values())


def test_every_harness_declares_the_image_that_carries_it():
    for harness in harnesses.HARNESSES.values():
        assert harness.image, harness.name
        assert harness.parameters, harness.name


# ---------------------------------------------------------------- the operation behind a run


def test_the_run_is_carried_by_a_registered_operation_type():
    """A benchmark reuses the operation machinery rather than growing a second lifecycle."""
    assert OPERATION_TYPE in OPERATION_TYPES
    prefix, wire_kind = OPERATION_TYPES[OPERATION_TYPE]
    assert prefix == DEPLOYMENT
    assert wire_kind == "benchmark"


# ---------------------------------------------------------------- reads


def test_runs_are_addressed_by_id_and_never_by_name():
    """A measurement has no name; what people reach for it by is the deployment, which is a filter."""
    query = sql(BenchmarkRepository(db=None).get_query(new_uid(BENCHMARK_RUN)))
    assert "ops_benchmark_run.uid" in query
    assert "ops_benchmark_run.name" not in query


@pytest.mark.asyncio
async def test_a_reference_that_is_not_a_run_id_is_not_looked_up(repo):
    assert await repo.get(new_uid(DEPLOYMENT)) is None
    assert await repo.get("not-an-id") is None


def test_the_list_is_newest_first(repo):
    query = sql(repo.list_query(limit=10))
    assert "ORDER BY ops_benchmark_run.seq DESC" in query


def test_the_deployment_filter_accepts_an_id_or_a_name(repo):
    assert "demand_deployment.uid" in sql(repo.list_query(limit=10, deployment=new_uid(DEPLOYMENT)))
    assert "demand_deployment.name" in sql(repo.list_query(limit=10, deployment="serve"))


def test_the_cluster_filter_accepts_an_id_or_a_name(repo):
    assert "supply_cluster.uid" in sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER)))
    assert "supply_cluster.name" in sql(repo.list_query(limit=10, cluster="iad-1"))


def test_a_run_names_the_recipe_and_operation_by_outer_join(repo):
    """Both outlive-able: a recipe may be deleted and an operation is only the errand."""
    query = sql(repo.get_query(new_uid(BENCHMARK_RUN)))
    assert query.count("LEFT OUTER JOIN") == 2


@pytest.mark.asyncio
async def test_an_unknown_harness_filter_is_refused_before_the_query():
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).list(harness="wrk")


@pytest.mark.asyncio
async def test_an_unknown_phase_filter_is_refused_before_the_query():
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).list(phase="finished")


# ---------------------------------------------------------------- the shape of the surface


def test_the_create_request_defaults_to_aiperf_and_no_parameters():
    data = BenchmarkRunCreateRequest(deployment="serve")
    assert data.harness == harnesses.DEFAULT_HARNESS
    assert data.parameters == {}


def test_the_harness_catalogue_is_served_by_the_plane():
    """The console renders its form from this; a second copy of the table is a second answer."""
    served = BenchmarkService(db=None).harnesses()
    assert {harness.name for harness in served} == set(harnesses.HARNESSES)
    aiperf = next(harness for harness in served if harness.name == "aiperf")
    assert {p.name for p in aiperf.parameters} == {p.name for p in harnesses.AIPERF.parameters}


def test_there_is_no_update_and_no_delete_route():
    """A measurement is not editable, and deleting one throws away the history worth keeping."""
    methods = {name for name, member in vars(BenchmarkRoutes).items() if hasattr(member, "fn")}
    assert "update_run" not in methods
    assert "delete_run" not in methods
    assert {"list_runs", "create_run", "get_run", "list_harnesses"} <= methods


def test_the_result_is_carried_as_an_opaque_dict():
    """No normalised metric columns: that schema is deferred until two harnesses have been read."""
    fields = {field.name: field.type for field in msgspec.structs.fields(BenchmarkRunSchema)}
    assert fields["summary"] == (dict | None)
    assert "ttft_p50" not in fields
    assert "throughput" not in fields
    assert "output_token_throughput" not in fields


def test_provenance_is_snapshotted_onto_the_run():
    """A recipe is mutable and a deployment is disposable; a joined run would drift off what it measured."""
    fields = {field.name: field.type for field in msgspec.structs.fields(BenchmarkRunSchema)}
    assert fields["provenance"] is BenchmarkProvenanceSchema
    snapshot = {field.name for field in msgspec.structs.fields(BenchmarkProvenanceSchema)}
    assert snapshot == {"model", "deployment_generation", "recipe_id", "recipe_version", "engine_args"}
