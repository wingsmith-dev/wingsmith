"""The service's assembly step: a backend's numbers into a response.

`_answer` never touches the session — it resolves a source, builds a request,
and shapes what comes back — so it is testable against an in-memory Cluster and
a stub backend. The scoping joins that do read the database live in the
database-marked suite.
"""

from unittest.mock import patch

import pytest
import msgspec

from app.db.models import Recipe
from app.db.models import Cluster
from app.db.models import Artifact
from app.modules.metrics import sources
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.backend import Curve
from app.modules.metrics.backend import Scope
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.services import MetricsService
from app.modules.metrics.services import served_model


UTILIZATION = BY_NAME["gpu.utilization"]
POWER = BY_NAME["gpu.power"]

SOURCES = "cluster-us|label=cluster-us,default=https://central:9090"


def cluster(name="cluster-us", uid="clu_01j9x2aaaaaaaaaaaaaaaaaaaa") -> Cluster:
    return Cluster(name=name, uid=uid)


def service() -> MetricsService:
    """The session is never reached by the paths under test, so it is not supplied."""
    return MetricsService(db=None)  # type: ignore[arg-type]


class StubBackend:
    def __init__(self, samples=None, breakdown=None, curves=None, raises=None):
        self._samples = samples or {}
        self._breakdown = breakdown or {}
        self._curves = curves or []
        self._raises = raises
        self.requests = []

    async def samples(self, request):
        self.requests.append(request)
        if self._raises:
            raise self._raises
        return self._samples

    async def breakdown(self, request):
        return self._breakdown

    async def curves(self, request):
        return self._curves


def with_backend(backend):
    return patch("app.modules.metrics.services.for_source", lambda source, timeout: backend)


def with_sources(value=SOURCES):
    return patch.object(sources.settings, "METRICS_SOURCES", value)


async def answer(svc, backend, scope, **kw):
    with with_sources(), with_backend(backend):
        return await svc._answer(
            cluster(),
            scope,
            "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
            kw.pop("metrics", (UTILIZATION,)),
            kw.pop("window", None),
            kw.pop("step", None),
            kw.pop("span", 3600),
            **kw,
        )


# --- an unconfigured cluster -----------------------------------------------


async def test_a_cluster_with_no_source_says_so_rather_than_answering_zero():
    """An empty panel with a reason beats a zero that reads as an idle GPU."""
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", ""), with_backend(StubBackend()):
        got = await svc._answer(cluster(), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600)

    assert got.unavailable
    assert "no metrics source configured" in got.unavailable
    assert "HANGAR_METRICS_SOURCES" in got.unavailable, "the message does not say how to fix it"
    assert got.source is None
    assert not got.samples


async def test_an_unconfigured_cluster_never_reaches_a_backend():
    backend = StubBackend()
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", ""), with_backend(backend):
        await svc._answer(cluster(), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600)
    assert not backend.requests


# --- the shape of an answer -------------------------------------------------


async def test_an_answer_identifies_the_scope_cluster_and_source():
    backend = StubBackend(samples={"gpu.utilization": 72.5})
    got = await answer(service(), backend, Scope(kind="cluster", cluster="cluster-us"))

    assert got.scope == "cluster"
    assert got.ref == "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
    assert got.cluster == "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
    assert got.source is not None
    assert got.unavailable is None


async def test_samples_carry_the_title_and_unit_the_registry_gives_them():
    backend = StubBackend(samples={"gpu.utilization": 72.5, "gpu.power": 410.0})
    got = await answer(service(), backend, Scope(kind="cluster"), metrics=(UTILIZATION, POWER))

    by_name = {s.metric: s for s in got.samples}
    assert by_name["gpu.utilization"].value == 72.5
    assert by_name["gpu.utilization"].unit == UTILIZATION.unit
    assert by_name["gpu.utilization"].title == UTILIZATION.title
    assert by_name["gpu.power"].value == 410.0


async def test_samples_stay_in_the_order_the_metrics_were_asked_for():
    """A panel's rows must not reshuffle between refreshes."""
    backend = StubBackend(samples={"gpu.power": 410.0, "gpu.utilization": 72.5})
    got = await answer(service(), backend, Scope(kind="cluster"), metrics=(UTILIZATION, POWER))
    assert [s.metric for s in got.samples] == ["gpu.utilization", "gpu.power"]


async def test_a_metric_with_no_series_is_reported_as_none_not_omitted():
    backend = StubBackend(samples={"gpu.utilization": None})
    got = await answer(service(), backend, Scope(kind="cluster"))
    assert [(s.metric, s.value) for s in got.samples] == [("gpu.utilization", None)]


async def test_asking_for_no_metrics_answers_without_touching_the_backend():
    backend = StubBackend()
    got = await answer(service(), backend, Scope(kind="cluster"), metrics=())
    assert not backend.requests
    assert got.unavailable is None
    assert not got.samples


# --- window and step --------------------------------------------------------


async def test_the_default_window_is_used_when_none_is_asked_for():
    backend = StubBackend(samples={})
    svc = service()
    got = await answer(svc, backend, Scope(kind="cluster"))
    assert got.window == svc.window
    assert backend.requests[0].window.lookback == svc.window


async def test_an_explicit_window_overrides_the_default_and_is_reported_back():
    backend = StubBackend(samples={})
    got = await answer(service(), backend, Scope(kind="cluster"), window="30m")
    assert got.window == "30m"
    assert backend.requests[0].window.lookback == "30m"


async def test_the_span_and_step_reach_the_request():
    backend = StubBackend(samples={})
    await answer(service(), backend, Scope(kind="cluster"), step="15s", span=900)
    assert backend.requests[0].window.step == "15s"
    assert backend.requests[0].window.span == 900


# --- breakdown --------------------------------------------------------------


async def test_a_breakdown_is_returned_sorted_so_a_panel_does_not_reshuffle():
    backend = StubBackend(
        samples={"gpu.utilization": 50.0},
        breakdown={"n-03": {"gpu.utilization": 30.0}, "n-01": {"gpu.utilization": 70.0}},
    )
    got = await answer(service(), backend, Scope(kind="cluster", breakdown="node"))
    assert [b.key for b in got.breakdown] == ["n-01", "n-03"]


async def test_a_breakdown_key_is_labelled_when_a_label_was_supplied():
    backend = StubBackend(samples={}, breakdown={"nod_01j9": {"gpu.utilization": 30.0}})
    got = await answer(
        service(),
        backend,
        Scope(kind="cluster", breakdown="node"),
        labels={"nod_01j9": "iad-1-n01"},
    )
    assert got.breakdown[0].label == "iad-1-n01"


async def test_a_breakdown_key_with_no_label_falls_back_to_the_key():
    backend = StubBackend(samples={}, breakdown={"GPU-aaa": {"gpu.utilization": 30.0}})
    got = await answer(service(), backend, Scope(kind="cluster", breakdown="device"), labels={})
    assert got.breakdown[0].label == "GPU-aaa"


async def test_no_breakdown_is_requested_when_the_scope_has_no_dimension():
    backend = StubBackend(samples={}, breakdown={"n-01": {"gpu.utilization": 1.0}})
    got = await answer(service(), backend, Scope(kind="cluster"))
    assert not got.breakdown


# --- series -----------------------------------------------------------------


async def test_series_are_only_fetched_when_asked_for():
    curve = Curve(metric="gpu.utilization", labels={}, points=((1.0, 2.0),))
    backend = StubBackend(samples={}, curves=[curve])

    without = await answer(service(), backend, Scope(kind="cluster"))
    assert not without.series

    got = await answer(service(), backend, Scope(kind="cluster"), with_series=True)
    assert len(got.series) == 1


async def test_a_series_carries_its_points_labels_title_and_unit():
    curve = Curve(
        metric="gpu.utilization",
        labels={"hostname": "iad-1-n01"},
        points=((1787158053.0, 10.0), (1787158113.0, 20.0)),
    )
    backend = StubBackend(samples={}, curves=[curve])
    got = await answer(service(), backend, Scope(kind="cluster"), with_series=True)

    series = got.series[0]
    assert series.metric == "gpu.utilization"
    assert series.title == UTILIZATION.title
    assert series.unit == UTILIZATION.unit
    assert series.labels == {"hostname": "iad-1-n01"}
    assert [(p.at, p.value) for p in series.points] == [(1787158053.0, 10.0), (1787158113.0, 20.0)]


# --- a store that cannot answer --------------------------------------------


async def test_a_store_that_is_down_becomes_a_reason_not_an_exception():
    """Hangar's inventory answer is still good; the panel says why it is empty."""
    backend = StubBackend(raises=MetricsUnavailable("cannot reach the metrics store for us"))
    got = await answer(service(), backend, Scope(kind="cluster"))

    assert got.unavailable == "cannot reach the metrics store for us"
    assert got.source is not None, "the source is still named, so the console can say which store"
    assert not got.samples


async def test_a_store_that_fails_midway_still_answers_with_a_reason():
    class HalfBroken(StubBackend):
        async def breakdown(self, request):
            raise MetricsUnavailable("query failed")

    backend = HalfBroken(samples={"gpu.utilization": 50.0})
    got = await answer(service(), backend, Scope(kind="cluster", breakdown="node"))
    assert got.unavailable == "query failed"


async def test_an_unavailable_store_is_never_raised_at_the_caller():
    backend = StubBackend(raises=MetricsUnavailable("down"))
    try:
        await answer(service(), backend, Scope(kind="cluster"))
    except MetricsUnavailable:  # pragma: no cover - the assertion is that this does not happen
        pytest.fail("a metrics outage was raised out of the service and would become a 5xx")


# --- scope construction -----------------------------------------------------


def test_a_scope_always_carries_the_cluster_label_its_source_is_narrowed_by():
    """The fleet remote-writes into one store holding four clusters."""
    source = sources.Source(cluster="us", url="https://prom", label="cluster-us")
    scope = service()._scope("node", source, node=("iad-1-n01",))
    assert scope.cluster == "cluster-us"
    assert scope.match == {"node": ("iad-1-n01",)}


def test_a_scope_with_no_source_carries_no_cluster():
    scope = service()._scope("node", None, node=("iad-1-n01",))
    assert scope.cluster == ""


def test_an_empty_match_dimension_is_dropped_rather_than_selecting_nothing():
    """A dimension present with no values would render a matcher that matches nothing."""
    source = sources.Source(cluster="us", url="https://prom", label="l")
    scope = service()._scope("deployment", source, worker=(), deployment=("gemma",))
    assert scope.match == {"deployment": ("gemma",)}


def test_a_breakdown_dimension_reaches_the_scope():
    source = sources.Source(cluster="us", url="https://prom", label="l")
    assert service()._scope("cluster", source, breakdown="node").breakdown == "node"


# --- the served model name --------------------------------------------------


def test_an_explicit_served_model_name_wins_because_the_operator_said_so():
    recipe = Recipe(name="gemma-4-31b-it", engine_args={"served_model_name": "gemma-custom"})
    artifact = Artifact(name="google/gemma-4-31B-it")
    assert served_model(recipe, artifact) == "gemma-custom"


def test_the_recipe_name_is_next_because_it_is_what_the_engine_emits():
    recipe = Recipe(name="gemma-4-31b-it", engine_args={})
    artifact = Artifact(name="google/gemma-4-31B-it")
    assert served_model(recipe, artifact) == "gemma-4-31b-it"


def test_the_artifact_is_the_last_resort_and_usually_matches_nothing():
    """It holds the repository a model is fetched from, not the name it serves under."""
    recipe = Recipe(name=None, engine_args={})
    artifact = Artifact(name="google/gemma-4-31B-it")
    assert served_model(recipe, artifact) == "google/gemma-4-31B-it"


def test_an_empty_served_model_name_falls_through_rather_than_selecting_nothing():
    recipe = Recipe(name="gemma-4-31b-it", engine_args={"served_model_name": ""})
    artifact = Artifact(name="google/gemma-4-31B-it")
    assert served_model(recipe, artifact) == "gemma-4-31b-it"


# --- configured sources -----------------------------------------------------


def test_the_source_listing_never_carries_a_token():
    """A listing is the most likely thing to end up in a screenshot."""
    with patch.object(sources.settings, "METRICS_SOURCES", "us|label=l|token=secret,default=https://c:9090"):
        listed = service().sources()
    assert listed
    for entry in listed:
        rendered = msgspec.json.encode(entry).decode()
        assert "secret" not in rendered, f"a token reached the listing: {rendered}"


# --- the deployment join ----------------------------------------------------


class OneRowSession:
    """Enough session to answer the deployment lookup with a single row."""

    def __init__(self, row):
        self._row = row

    async def execute(self, query):
        return self

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return self._row


DYNAMO_LABELS = {"dynamo-namespace": "dynamo", "dynamo-worker-hash": "891b8dda"}


async def deployment_scope(labels: dict) -> Scope:
    """The scope `deployment()` builds, with the store stubbed out."""
    from app.db.models import Workload
    from app.db.models import Deployment

    deployment = Deployment(uid="dep_01j9x2aaaaaaaaaaaaaaaaaaaa", cluster_id=None)
    workload = Workload(labels=labels)
    recipe = Recipe(name="gemma-4-31b-it", engine="vllm", engine_args={})
    artifact = Artifact(name="google/gemma-4-31B-it")
    svc = MetricsService(db=OneRowSession((deployment, workload, recipe, artifact)))  # type: ignore[arg-type]
    backend = StubBackend()
    with (
        patch.object(sources.settings, "METRICS_SOURCES", SOURCES),
        patch("app.modules.metrics.services.for_source", return_value=backend),
        patch.object(svc, "_cluster", return_value=cluster()),
    ):
        await svc.deployment("dep_01j9x2aaaaaaaaaaaaaaaaaaaa")
    return backend.requests[0].scope


@pytest.mark.asyncio
async def test_the_worker_join_reads_the_labels_off_the_workload():
    """Labels live on the workload; reaching for `Deployment.labels` was a 500."""
    scope = await deployment_scope(DYNAMO_LABELS)
    assert scope.match == {"worker": ("dynamo-891b8dda",)}


@pytest.mark.asyncio
async def test_an_unobserved_worker_falls_back_to_the_served_model():
    scope = await deployment_scope({})
    assert scope.match == {"deployment": ("gemma-4-31b-it",)}
