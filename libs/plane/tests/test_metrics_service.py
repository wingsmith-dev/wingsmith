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
from app.db.models import Endpoint
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.modules.metrics import sources
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.backend import Curve
from app.modules.metrics.backend import Scope
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.registry import family
from app.modules.metrics.services import MetricsService
from app.modules.metrics.services import _merge
from app.modules.metrics.services import served_model


UTILIZATION = BY_NAME["gpu.utilization"]
POWER = BY_NAME["gpu.power"]

SOURCES = "us-cluster-1|label=us-cluster-1,default=https://central:9090"
SHARED = "default=https://central:9090|token=secret"


def cluster(
    name="us-cluster-1",
    uid="clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
    metrics_label="us-cluster-1",
) -> Cluster:
    return Cluster(name=name, uid=uid, metrics_label=metrics_label)


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


async def test_a_plane_with_no_store_says_so_rather_than_answering_zero():
    """An empty panel with a reason beats a zero that reads as an idle GPU."""
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", ""), with_backend(StubBackend()):
        got = await svc._answer(cluster(), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600)

    assert got.unavailable
    assert "no metrics store" in got.unavailable
    assert "HANGAR_METRICS_SOURCES" in got.unavailable, "the message does not say how to fix it"
    assert got.source is None
    assert not got.samples


async def test_an_unlabelled_cluster_reports_no_source_even_with_a_shared_store():
    """ADR 0008: a guessed label against the shared store reads as a healthy idle fleet."""
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", SHARED), with_backend(StubBackend()):
        got = await svc._answer(
            cluster(metrics_label=None), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600
        )

    assert got.source is None
    assert got.unavailable
    assert "no metrics label" in got.unavailable
    assert "us-cluster-1" in got.unavailable
    assert not got.samples


async def test_an_unlabelled_cluster_never_reaches_a_backend():
    backend = StubBackend()
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", SHARED), with_backend(backend):
        await svc._answer(cluster(metrics_label=None), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600)
    assert not backend.requests


async def test_an_unconfigured_cluster_never_reaches_a_backend():
    backend = StubBackend()
    svc = service()
    with patch.object(sources.settings, "METRICS_SOURCES", ""), with_backend(backend):
        await svc._answer(cluster(), Scope(kind="cluster"), "ref", (UTILIZATION,), None, None, 3600)
    assert not backend.requests


# --- the shape of an answer -------------------------------------------------


async def test_an_answer_identifies_the_scope_cluster_and_source():
    backend = StubBackend(samples={"gpu.utilization": 72.5})
    got = await answer(service(), backend, Scope(kind="cluster", cluster="us-cluster-1"))

    assert got.scope == "cluster"
    assert got.ref == "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
    assert got.cluster == "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
    assert got.source is not None
    assert got.unavailable is None


async def test_an_answer_for_no_single_cluster_still_names_the_store_it_came_from():
    """A workload, the fleet and an endpoint each span or skip clusters.

    `cluster: null` says the numbers belong to no one cluster; the source still
    has to be there, because a caller must be able to tell which store answered
    from an answer that names no cluster at all.
    """
    backend = StubBackend(samples={"gpu.utilization": 72.5})
    svc = service()
    with with_sources(), with_backend(backend):
        got = await svc._answer_source(
            sources.Source(cluster="us", url="https://central:9090", label="us-cluster-1"),
            None,
            Scope(kind="workload"),
            "wl_01j9x2aaaaaaaaaaaaaaaaaaaa",
            (UTILIZATION,),
            None,
            None,
            3600,
        )

    assert got.cluster is None
    assert got.source is not None
    assert got.source.url == "https://central:9090"
    assert got.unavailable is None
    assert [(s.metric, s.value) for s in got.samples] == [("gpu.utilization", 72.5)]


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
    source = sources.Source(cluster="us", url="https://prom", label="us-cluster-1")
    scope = service()._scope("node", source, node=("iad-1-n01",))
    assert scope.cluster == "us-cluster-1"
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


# --- resolving a store for a cluster ----------------------------------------


def resolve(spec, **kw):
    with patch.object(sources.settings, "METRICS_SOURCES", spec):
        return sources.resolve(cluster(**kw))


def test_a_labelled_cluster_is_answered_by_the_shared_store():
    """The column carries the label; the environment carries only the endpoint."""
    got = resolve(SHARED, metrics_label="cluster-a")
    assert got is not None
    assert got.url == "https://central:9090"
    assert got.label == "cluster-a"
    assert got.token == "secret", "the shared endpoint's credential is inherited"


def test_an_unlabelled_cluster_has_no_store_at_all():
    assert resolve(SHARED, metrics_label=None) is None


def test_a_blank_label_is_treated_as_no_label_rather_than_matching_nothing():
    assert resolve(SHARED, metrics_label="") is None


def test_an_explicit_entry_overrides_the_column():
    """A wrong column is fixable from the deployment, without a database write."""
    got = resolve(f"{SHARED},us-cluster-1|label=corrected", metrics_label="stale")
    assert got is not None
    assert got.label == "corrected"


def test_an_entry_keyed_by_uid_also_overrides_the_column():
    got = resolve(f"{SHARED},clu_01j9x2aaaaaaaaaaaaaaaaaaaa|label=corrected", metrics_label="stale")
    assert got is not None
    assert got.label == "corrected"


def test_a_dedicated_store_entry_still_wins_and_still_carries_no_matcher():
    """The pre-`remote_write` form: one Prometheus holding one cluster (ADR 0008)."""
    got = resolve(f"{SHARED},us-cluster-1=https://prom.us:9090", metrics_label="ignored")
    assert got is not None
    assert got.url == "https://prom.us:9090"
    assert got.label == "", "a store holding one cluster has no cluster label to match on"
    assert got.token == "", "a dedicated store does not inherit the shared endpoint's credential"


def test_a_labelled_cluster_with_no_shared_endpoint_has_no_store():
    """A label names which series, not where they are; without an endpoint it answers nothing."""
    assert resolve("", metrics_label="us-cluster-1") is None


# --- configured sources -----------------------------------------------------


class RowsSession:
    """Enough session to answer the registry walk the listing does."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, query):
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows


async def test_the_source_listing_never_carries_a_token():
    """A listing is the most likely thing to end up in a screenshot."""
    svc = MetricsService(db=RowsSession([cluster()]))  # type: ignore[arg-type]
    with patch.object(sources.settings, "METRICS_SOURCES", "us|label=l|token=secret,default=https://c:9090"):
        listed = await svc.sources()
    assert listed
    for entry in listed:
        rendered = msgspec.json.encode(entry).decode()
        assert "secret" not in rendered, f"a token reached the listing: {rendered}"


async def test_the_source_listing_names_a_cluster_answered_only_by_its_column():
    svc = MetricsService(db=RowsSession([cluster(metrics_label="cluster-a")]))  # type: ignore[arg-type]
    with patch.object(sources.settings, "METRICS_SOURCES", SHARED):
        listed = await svc.sources()
    assert [entry.cluster for entry in listed] == ["us-cluster-1"]


async def test_the_source_listing_omits_a_cluster_with_no_label():
    svc = MetricsService(db=RowsSession([cluster(metrics_label=None)]))  # type: ignore[arg-type]
    with patch.object(sources.settings, "METRICS_SOURCES", SHARED):
        assert await svc.sources() == []


async def test_an_env_entry_matching_no_cluster_is_still_listed():
    """A typo in one is exactly what this endpoint gets checked for."""
    svc = MetricsService(db=RowsSession([]))  # type: ignore[arg-type]
    with patch.object(sources.settings, "METRICS_SOURCES", f"{SHARED},typo|label=l"):
        listed = await svc.sources()
    assert [entry.cluster for entry in listed] == ["typo"]


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


# --- the workload scope ------------------------------------------------------


class ScriptedSession:
    """Answers a fixed sequence of queries, in the order the service issues them.

    A workload answer is three reads — the workload, the clusters it landed in,
    the devices it holds — so a session that answers every query the same way
    cannot exercise it.
    """

    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, query):
        return ScriptedResult(self._results.pop(0))


class ScriptedResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self


class SequenceBackend(StubBackend):
    """A different set of numbers per call, so two clusters can answer differently."""

    def __init__(self, *answers, curves=None):
        super().__init__(curves=curves)
        self._answers = list(answers)

    async def samples(self, request):
        self.requests.append(request)
        return self._answers.pop(0)


WORKLOAD = "wkl_01j9x2aaaaaaaaaaaaaaaaaaaa"
DEPLOYMENT = "dep_01j9x2aaaaaaaaaaaaaaaaaaaa"
US = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"
AP = "clu_01j9x2bbbbbbbbbbbbbbbbbbbb"


def workload_row(labels=None, engine="vllm"):
    from app.db.models import Workload

    workload = Workload(uid=WORKLOAD, name="gemma-serve", labels=DYNAMO_LABELS if labels is None else labels)
    recipe = Recipe(name="gemma-4-31b-it", engine=engine, engine_args={})
    artifact = Artifact(name="google/gemma-4-31B-it")
    return (workload, recipe, artifact)


def workload_service(*results) -> MetricsService:
    return MetricsService(db=ScriptedSession(*results))  # type: ignore[arg-type]


def two_clusters():
    return [
        cluster(name="us-cluster-1", uid=US, metrics_label="us-cluster-1"),
        cluster(name="ap-cluster-1", uid=AP, metrics_label="ap-cluster-1"),
    ]


async def workload_answer(svc, backend, ref=WORKLOAD, **kw):
    with with_sources(SHARED), with_backend(backend):
        return await svc.workload(ref, **kw)


async def test_a_workload_answer_belongs_to_no_single_cluster():
    """It is the one object that spans them, so a cluster on the answer would be a lie."""
    svc = workload_service([workload_row()], two_clusters(), [])
    backend = SequenceBackend({"inference.requests_running": 1.0}, {"inference.requests_running": 2.0})
    got = await workload_answer(svc, backend)

    assert got.scope == "workload"
    assert got.ref == WORKLOAD
    assert got.cluster is None
    assert got.source is not None, "the store that answered is still named"


async def test_a_two_cluster_workload_adds_its_summed_metrics_together():
    """Requests running in two regions is the total; either half alone is wrong."""
    svc = workload_service([workload_row()], two_clusters(), [])
    backend = SequenceBackend(
        {"inference.requests_running": 3.0, "inference.output_tokens_per_second": 100.0},
        {"inference.requests_running": 5.0, "inference.output_tokens_per_second": 40.0},
    )
    got = await workload_answer(svc, backend)

    merged = {s.metric: s.value for s in got.samples}
    assert merged["inference.requests_running"] == 8.0
    assert merged["inference.output_tokens_per_second"] == 140.0


async def test_each_cluster_curve_says_which_cluster_it_came_from():
    """Two clusters' curves for one metric are otherwise indistinguishable.

    `sum(rate(...))` returns no labels, so both halves arrive as the same metric
    with the same title and an empty label set. A chart keying lines off that draws
    one and silently discards the other, and the one it keeps looks like the whole
    workload.
    """
    svc = workload_service([workload_row()], two_clusters(), [])
    curve = Curve(metric="inference.output_tokens_per_second", labels={}, points=((1.0, 2.0),))
    backend = SequenceBackend({}, {}, curves=[curve])
    got = await workload_answer(svc, backend, series=True)

    assert len(got.series) == 2
    assert [line.labels["cluster"] for line in got.series] == [
        "us-cluster-1",
        "ap-cluster-1",
    ]


async def test_a_two_cluster_workload_keeps_one_breakdown_row_per_cluster():
    svc = workload_service([workload_row()], two_clusters(), [])
    backend = SequenceBackend({"inference.requests_running": 3.0}, {"inference.requests_running": 5.0})
    got = await workload_answer(svc, backend)

    assert [(row.key, row.label) for row in got.breakdown] == [
        (US, "us-cluster-1"),
        (AP, "ap-cluster-1"),
    ]
    assert [row.samples[0].value for row in got.breakdown] == [3.0, 5.0]


async def test_a_percentile_is_left_out_of_a_two_cluster_answer_rather_than_averaged():
    """Two p95s have no mean, and the store that would know holds neither set of buckets.

    The console reads "not measured" for the merged cell and both real numbers
    are still in the breakdown, which is the honest version of this answer.
    """
    svc = workload_service([workload_row()], two_clusters(), [])
    backend = SequenceBackend({"inference.ttft": 0.4}, {"inference.ttft": 0.9})
    got = await workload_answer(svc, backend)

    assert "inference.ttft" not in {s.metric for s in got.samples}
    assert [row.samples[0].value for row in got.breakdown] == [0.4, 0.9]


async def test_a_metric_no_cluster_had_a_series_for_stays_none_rather_than_vanishing():
    svc = workload_service([workload_row()], two_clusters(), [])
    backend = SequenceBackend({"inference.ttft": None}, {"inference.ttft": None})
    got = await workload_answer(svc, backend)
    assert ("inference.ttft", None) in [(s.metric, s.value) for s in got.samples]


async def test_a_single_cluster_workload_reads_the_same_as_its_one_deployment():
    """With one cluster there is nothing to merge, so the two answers must agree."""
    from app.db.models import Deployment

    numbers = {"inference.ttft": 0.42, "inference.requests_running": 3.0}
    row = workload_row()
    svc = workload_service([row], [cluster()], [])
    workload_backend = SequenceBackend(dict(numbers))
    from_workload = await workload_answer(svc, workload_backend)

    other = MetricsService(db=OneRowSession((Deployment(uid=DEPLOYMENT, cluster_id=None), *row)))  # type: ignore[arg-type]
    deployment_backend = StubBackend(samples=dict(numbers))
    with (
        with_sources(SHARED),
        with_backend(deployment_backend),
        patch.object(other, "_cluster", return_value=cluster()),
    ):
        from_deployment = await other.deployment(DEPLOYMENT)

    assert [(s.metric, s.value) for s in from_workload.samples] == [
        (s.metric, s.value) for s in from_deployment.samples
    ]
    assert workload_backend.requests[0].scope.match == deployment_backend.requests[0].scope.match


async def test_the_serving_family_falls_back_to_the_served_model_exactly_as_a_deployment_does():
    svc = workload_service([workload_row(labels={})], [cluster()], [])
    backend = SequenceBackend({})
    await workload_answer(svc, backend)
    assert backend.requests[0].scope.match == {"deployment": ("gemma-4-31b-it",)}


async def test_the_cards_are_selected_by_the_devices_the_workload_holds_now():
    """The strongest join in the module: a UUID the hardware itself issued."""
    svc = workload_service([workload_row()], [cluster()], [(None, "GPU-bbb"), (None, "GPU-aaa")])
    backend = SequenceBackend({"gpu.utilization": 71.0}, {"inference.requests_running": 2.0})
    got = await workload_answer(svc, backend)

    assert backend.requests[0].scope.match == {"device": ("GPU-aaa", "GPU-bbb")}
    assert {s.metric for s in got.samples} == {"gpu.utilization", "inference.requests_running"}


async def test_a_workload_holding_no_devices_is_asked_nothing_about_gpus():
    """No live allocation is not an idle card, so the gpu samples are absent, not zero."""
    svc = workload_service([workload_row()], [cluster()], [])
    backend = SequenceBackend({"inference.requests_running": 2.0})
    got = await workload_answer(svc, backend)

    assert len(backend.requests) == 1
    assert not [s for s in got.samples if s.metric.startswith("gpu.")]


async def test_an_engine_with_no_dialect_says_so_and_never_reaches_a_store():
    svc = workload_service([workload_row(engine="tensorrt-llm")])
    backend = SequenceBackend()
    got = await workload_answer(svc, backend)

    assert got.unavailable == "no metric dialect registered for engine 'tensorrt-llm'"
    assert not backend.requests
    assert not got.samples


async def test_a_workload_placed_nowhere_says_so_rather_than_answering_zero():
    svc = workload_service([workload_row()], [])
    got = await workload_answer(svc, SequenceBackend())
    assert got.unavailable == "this workload is not deployed to any cluster"
    assert not got.samples


async def test_a_workload_whose_clusters_cannot_be_measured_carries_their_reason():
    svc = workload_service([workload_row()], [cluster(metrics_label=None)], [])
    got = await workload_answer(svc, SequenceBackend())

    assert got.unavailable is not None
    assert "no metrics label" in got.unavailable
    assert not got.samples
    assert not got.breakdown


async def test_a_bare_name_in_two_projects_names_the_candidates_rather_than_guessing():
    """The same rule `GET /v1/workloads/{ref}` follows: one of two is not a default."""
    svc = workload_service(
        [workload_row(), workload_row()],
        [(WORKLOAD, "team-a"), ("wkl_01j9x2cccccccccccccccccccc", "team-b")],
    )
    with pytest.raises(Invalid) as raised:
        await workload_answer(svc, SequenceBackend(), ref="gemma-serve")

    assert raised.value.detail["candidates"] == [
        {"id": WORKLOAD, "project": "team-a"},
        {"id": "wkl_01j9x2cccccccccccccccccccc", "project": "team-b"},
    ]


async def test_an_unknown_workload_is_a_404():
    svc = workload_service([])
    with pytest.raises(NotFound):
        await workload_answer(svc, SequenceBackend(), ref="nope")


# --- merging across clusters -------------------------------------------------


def test_a_temperature_takes_the_worst_cluster_not_the_mean_of_them():
    merged = _merge((BY_NAME["gpu.temperature"],), [{"gpu.temperature": 61.0}, {"gpu.temperature": 89.0}])
    assert merged == {"gpu.temperature": 89.0}


def test_a_utilization_two_clusters_reported_is_dropped_rather_than_averaged():
    """A mean of two means is only a mean if the clusters were the same size.

    Eight devices at 90% in one cluster and one device at 10% in another average
    to 50%, which is true of no device the workload holds — and it is the figure a
    person would read as the workload's utilization. Both real numbers are in the
    breakdown; the merged tile says nothing instead of saying that.
    """
    merged = _merge((UTILIZATION,), [{"gpu.utilization": 90.0}, {"gpu.utilization": 10.0}, {}])
    assert merged == {}


def test_a_single_cluster_utilization_survives_because_nothing_was_crossed():
    merged = _merge((UTILIZATION,), [{"gpu.utilization": 40.0}, {}])
    assert merged == {"gpu.utilization": 40.0}


def test_a_throughput_still_adds_because_it_is_true_of_the_union():
    merged = _merge(
        (BY_NAME["inference.output_tokens_per_second"],),
        [{"inference.output_tokens_per_second": 120.0}, {"inference.output_tokens_per_second": 30.0}],
    )
    assert merged == {"inference.output_tokens_per_second": 150.0}


def test_a_single_cluster_percentile_survives_because_nothing_was_merged():
    merged = _merge((BY_NAME["inference.ttft_p99"],), [{"inference.ttft_p99": 1.5}, {}])
    assert merged == {"inference.ttft_p99": 1.5}


def test_a_metric_nobody_measured_stays_none_rather_than_vanishing():
    """Absent and unmeasured are different answers; only one of them is a gap."""
    merged = _merge((UTILIZATION,), [{"gpu.utilization": None}, {"gpu.utilization": None}])
    assert merged == {"gpu.utilization": None}


# --- the endpoint and serving scopes -----------------------------------------

ENDPOINT = "end_01j9x2aaaaaaaaaaaaaaaaaaaa"


def endpoint_service(*results) -> MetricsService:
    return MetricsService(db=ScriptedSession(*results))  # type: ignore[arg-type]


async def test_an_endpoint_is_measured_at_the_front_door_and_by_its_uid():
    """`hangar_endpoint` is the uid the gateway stamped, not the name it was called by.

    An endpoint never reaches a cluster, so nothing in a cluster can be asked
    about one; the join is against the hop that resolved it, and a rename must not
    move the series.
    """
    svc = endpoint_service([Endpoint(uid=ENDPOINT, name="gemma-chat")])
    backend = StubBackend(samples={"serving.availability": 99.95})
    with with_sources(SHARED), with_backend(backend):
        got = await svc.endpoint("gemma-chat")

    assert backend.requests[0].scope.match == {"endpoint": (ENDPOINT,)}
    assert {m.name for m in backend.requests[0].metrics} == {m.name for m in family("serving")}
    assert got.scope == "endpoint"
    assert got.ref == ENDPOINT
    assert got.cluster is None, "an endpoint is global; naming a cluster would be a lie"
    assert [(s.metric, s.value) for s in got.samples] == [("serving.availability", 99.95)]


async def test_an_unknown_endpoint_is_a_404():
    svc = endpoint_service([])
    with with_sources(SHARED), with_backend(StubBackend()), pytest.raises(NotFound):
        await svc.endpoint("nope")


async def test_the_served_surface_belongs_to_no_cluster_and_breaks_down_by_endpoint():
    """The total comes from the front door, not from adding up a page of endpoints.

    A tile summed over whatever endpoints the console had loaded is a total of
    that page and moves when somebody pages, which is why this is one query.
    """
    svc = endpoint_service([(ENDPOINT, "gemma-chat")])
    backend = StubBackend(
        samples={"serving.requests_per_second": 12.0},
        breakdown={ENDPOINT: {"serving.requests_per_second": 12.0}},
    )
    with with_sources(SHARED), with_backend(backend):
        got = await svc.serving()

    assert got.scope == "serving"
    assert got.ref == "serving"
    assert got.cluster is None
    assert got.source is not None, "the store that answered is still named"
    assert backend.requests[0].scope.match == {}
    assert backend.requests[0].scope.breakdown == "endpoint"
    assert [(b.key, b.label) for b in got.breakdown] == [(ENDPOINT, "gemma-chat")]


async def test_the_gateways_reserved_keys_are_labelled_rather_than_dropped():
    """`_unknown` growing is a rename nobody told the callers about.

    A breakdown that filters the two reserved keys out cannot show that, and
    `_catalog` traffic would silently vanish from a total that included it.
    """
    svc = endpoint_service([(ENDPOINT, "gemma-chat")])
    backend = StubBackend(
        breakdown={
            ENDPOINT: {"serving.requests_per_second": 12.0},
            "_catalog": {"serving.requests_per_second": 0.5},
            "_unknown": {"serving.requests_per_second": 3.0},
        },
    )
    with with_sources(SHARED), with_backend(backend):
        got = await svc.serving()

    labels = {b.key: b.label for b in got.breakdown}
    assert labels["_catalog"] == "Model catalogue"
    assert labels["_unknown"] == "Unrecognised model"


async def test_the_gateway_is_never_narrowed_by_a_cluster_label():
    """`cluster` on a gateway series is the destination, not the sender.

    It is the empty string for exactly the requests that matter most — the ones no
    cluster ever saw, because no upstream was reachable. Narrowing the query by any
    cluster value would drop those, which would turn a total outage into "no
    traffic". Telling two gateways apart needs a label of the gateway's own.
    """
    svc = endpoint_service([Endpoint(uid=ENDPOINT, name="gemma-chat")])
    backend = StubBackend(samples={"serving.requests_per_second": 12.0})
    with with_sources(SHARED), with_backend(backend):
        await svc.endpoint(ENDPOINT)

    scope = backend.requests[0].scope
    assert scope.cluster == ""
    assert "cluster" not in scope.match


async def test_no_gateway_store_says_so_rather_than_reporting_full_availability():
    """Nothing scrapes the gateway's metrics port yet, so this is the day-one answer.

    `infra/80-gateway.yaml` exposes the port and no ServiceMonitor reads it. Until
    one does, both routes have to answer with a reason and no samples — an
    availability of 100% here would be a number nothing measured, and 0% would be
    an outage nothing observed.
    """
    svc = endpoint_service([Endpoint(uid=ENDPOINT, name="gemma-chat")])
    with with_sources(""), with_backend(StubBackend(samples={"serving.availability": 100.0})):
        one = await svc.endpoint(ENDPOINT)
        every = await svc.serving()

    for got in (one, every):
        assert got.samples == []
        assert got.breakdown == []
        assert got.source is None
        assert got.unavailable
        assert "HANGAR_METRICS_SOURCES" in got.unavailable
