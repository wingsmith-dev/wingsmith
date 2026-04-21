import re

import pytest

from app.core.devices import DIALECT_DCGM
from app.core.devices import DeviceClass
from app.core.devices import DeviceTable
from app.modules.metrics import promql
from app.modules.metrics import sources
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.promql import LABELS
from app.modules.metrics.promql import DIALECT
from app.modules.metrics.promql import render
from app.modules.metrics.promql import matcher
from app.modules.metrics.promql import selector
from app.modules.metrics.promql import worker_identity
from app.modules.metrics.routes import MetricsRoutes
from app.modules.metrics.backend import Scope
from app.modules.metrics.backend import Window
from app.modules.metrics.backend import Backend
from app.modules.metrics.backend import Request
from app.modules.metrics.backend import for_source
from app.modules.metrics.schemas import SampleSchema
from app.modules.metrics.schemas import MetricsSchema
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.registry import ENGINES
from app.modules.metrics.registry import Metric
from app.modules.metrics.registry import family
from app.modules.metrics.services import MetricsService
from app.modules.metrics.services import served_model
from app.modules.metrics.prometheus import PrometheusBackend


WRITE_METHODS = ("create", "update", "delete", "patch", "post", "add", "remove")

CENTRAL = sources.Source(cluster="us", url="https://prom", label="cluster-us")


class FakeClient:
    """Stands in for the transport, so a backend can be exercised without a store."""

    def __init__(self, answers: dict[str, list[dict]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def instant(self, expression: str) -> list[dict]:
        self.asked.append(expression)
        return self.answers.get(expression, [])

    async def range(self, expression: str, start: float, end: float, step: str) -> list[dict]:
        self.asked.append(expression)
        return self.answers.get(expression, [])


def backend_with(answers: dict[str, list[dict]]) -> tuple[PrometheusBackend, FakeClient]:
    backend = PrometheusBackend(CENTRAL, 1.0)
    client = FakeClient(answers)
    backend.client = client
    return backend, client


def sample(value: str) -> list[dict]:
    return [{"metric": {}, "value": [0.0, value]}]


def test_service_exposes_no_write_methods():
    for name in WRITE_METHODS:
        assert not hasattr(MetricsService, name)


def test_controller_only_reads():
    handlers = [h for h in vars(MetricsRoutes).values() if hasattr(h, "http_methods")]
    assert handlers
    for handler in handlers:
        assert set(handler.http_methods) == {"GET"}


def test_unavailable_is_not_a_server_error():
    """A dead Prometheus is not a broken plane, and 503 says so."""
    assert MetricsUnavailable.status == 503
    assert MetricsUnavailable.code == "metrics_unavailable"


def test_source_never_leaks_its_token():
    parsed = sources.parse("us=https://prom.us:9090|token=secret")
    assert parsed["us"].token == "secret"
    assert "secret" not in str(parsed["us"].public())
    assert set(parsed["us"].public()) == {"cluster", "url", "kind"}


def test_a_dedicated_endpoint_per_cluster_still_parses():
    """The per-cluster stores are being decommissioned, not gone."""
    parsed = sources.parse("us=https://prom.us:9090/,ap=https://prom.ap:9090|kind=prometheus")
    assert set(parsed) == {"us", "ap"}
    assert parsed["us"].url == "https://prom.us:9090"
    assert parsed["ap"].kind == "prometheus"
    assert parsed["us"].label == ""


def test_the_shared_store_is_one_endpoint_and_a_label_per_cluster():
    parsed = sources.parse(
        "default=https://central:9090|token=t,us|label=cluster-us,ap|label=cluster-ap"
    )
    assert set(parsed) == {"us", "ap"}
    assert parsed["us"].url == "https://central:9090"
    assert parsed["us"].token == "t"
    assert parsed["us"].label == "cluster-us"
    assert parsed["ap"].label == "cluster-ap"


def test_an_entry_on_the_shared_store_is_never_unlabelled():
    """An unscoped query against the central store reads four fleets as one."""
    parsed = sources.parse("default=https://central:9090,us")
    assert parsed["us"].label == "us"


def test_a_cluster_with_no_entry_has_no_source_even_when_a_default_exists():
    """Reporting plainly beats guessing a label and rendering an idle fleet."""
    parsed = sources.parse("default=https://central:9090,us|label=cluster-us")
    assert sources.Source(cluster="ap", url="https://central:9090") not in parsed.values()
    assert "ap" not in parsed


def test_malformed_source_entries_are_dropped_not_raised():
    assert sources.parse("nonsense,us=,=https://x,us=https://prom:9090") == {
        "us": sources.Source(cluster="us", url="https://prom:9090")
    }


def test_selector_quotes_are_escaped():
    assert promql.escape('a"b') == 'a\\"b'
    assert matcher("hostname", ('no"pe',)) == 'hostname="no\\"pe"'


def test_selector_switches_to_regex_for_many_values():
    assert matcher("hostname", ("a",)) == 'hostname="a"'
    assert matcher("hostname", ("a", "b")) == 'hostname=~"a|b"'
    assert matcher("hostname", ()) == ""


def test_every_scope_carries_a_cluster_matcher():
    """One central store holds four clusters, two of them databases."""
    for kind, match in (
        ("cluster", {}),
        ("node", {"node": ("n1",)}),
        ("pool", {"node": ("n1", "n2")}),
        ("device", {"device": ("GPU-1",)}),
        ("deployment", {"deployment": ("gpt-oss-120b",)}),
    ):
        where = selector(Scope(kind=kind, cluster="cluster-us", match=match))
        assert where.startswith('cluster="cluster-us"'), kind


def test_a_dedicated_store_gets_no_cluster_matcher():
    """A Prometheus holding one cluster has no such label to match on."""
    assert selector(Scope(kind="node", cluster="", match={"node": ("n1",)})) == 'hostname="n1"'


def test_node_is_selected_by_the_label_the_exporter_actually_emits():
    """`Hostname` matched zero series and emptied every node and pool panel."""
    assert LABELS["node"] == "hostname"
    assert LABELS["device"] == "UUID"
    assert LABELS["deployment"] == "model_name"
    assert LABELS["cluster"] == "cluster"


def test_bare_series_metrics_are_aggregated_and_placeholders_are_substituted():
    util = BY_NAME["gpu.utilization"]
    assert render(util, "DCGM_FI_DEV_GPU_UTIL", 'hostname="n1"', "5m") == 'avg(DCGM_FI_DEV_GPU_UTIL{hostname="n1"})'
    assert render(util, "DCGM_FI_DEV_GPU_UTIL", "", "5m", by="UUID") == "avg by (UUID) (DCGM_FI_DEV_GPU_UTIL)"

    ttft = BY_NAME["inference.ttft"]
    rendered = render(ttft, DIALECT["inference.ttft"].candidates[0], 'model_name="q"', "5m")
    assert "$SELECTOR" not in rendered and "$WINDOW" not in rendered and "$SERIES" not in rendered
    assert 'model_name="q"' in rendered and "[5m]" in rendered


def test_temperature_takes_the_max_not_the_mean():
    """A pool with one card at 89C is not a 62C pool."""
    assert BY_NAME["gpu.temperature"].aggregate == "max"
    assert BY_NAME["gpu.utilization"].aggregate == "avg"
    assert BY_NAME["gpu.power"].aggregate == "sum"


def test_registry_covers_the_asks():
    gpu = {m.name for m in family("gpu")}
    inference = {m.name for m in family("inference")}
    assert {"gpu.utilization", "gpu.temperature", "gpu.sm_clock", "gpu.throttle_reasons"} <= gpu
    assert {
        "inference.ttft",
        "inference.tpot",
        "inference.output_tokens_per_second",
        "inference.kv_cache_utilization",
    } <= inference


def test_metric_names_are_unique_across_dialects():
    names = [m.name for engine in ENGINES.values() for m in engine.metrics]
    assert len(names) == len(set(names))


def test_the_registry_is_vendor_neutral():
    """The API's vocabulary must not go blank because an engine renamed a series."""
    for metric in BY_NAME.values():
        assert not any("DCGM" in str(v) or "vllm" in str(v) for v in vars(metric).values())


def test_every_registry_metric_has_a_prometheus_expression():
    assert set(DIALECT) == set(BY_NAME)


def test_only_real_substitution_tokens_appear_in_a_template():
    """A token the renderer does not know survives into the query and matches nothing."""
    known = {"$SERIES", "$SELECTOR", "$WINDOW"}
    for name, expression in DIALECT.items():
        tokens = set(re.findall(r"\$[A-Z_]+", expression.template))
        assert tokens <= known, f"{name} uses {sorted(tokens - known)}"


def test_the_two_throughput_metrics_are_different_measurements():
    """Aggregate throughput rises with scale; per-stream speed does not.

    An acceptance floor of "30 tokens/second" means the second one, and reading
    the first gives a check that passes under every possible condition.
    """
    aggregate = BY_NAME["inference.output_tokens_per_second"]
    per_stream = BY_NAME["inference.output_tokens_per_stream"]
    assert aggregate.aggregate == "sum"
    assert per_stream.aggregate == "avg"
    assert DIALECT[aggregate.name].candidates != DIALECT[per_stream.name].candidates


def test_named_ttft_percentiles_render_the_percentile_they_are_named_for():
    for name, quantile in (("inference.ttft_p50", "0.50"), ("inference.ttft_p90", "0.90"), ("inference.ttft", "0.95")):
        assert f"histogram_quantile({quantile}," in DIALECT[name].template


def test_renamed_upstream_series_are_kept_as_candidates():
    """An upstream rename must degrade to a fallback, not to an empty panel."""
    assert DIALECT["inference.tpot"].candidates == (
        "vllm:time_per_output_token_seconds_bucket",
        "vllm:inter_token_latency_seconds_bucket",
    )
    assert DIALECT["inference.kv_cache_utilization"].candidates[1] == "vllm:kv_cache_usage_perc"


def test_fields_the_exporter_does_not_emit_yet_are_kept():
    """They read as no data today and start working when the field csv grows."""
    assert DIALECT["gpu.throttle_reasons"].candidates[0] == "DCGM_FI_DEV_CLOCK_THROTTLE_REASONS"
    assert DIALECT["gpu.ecc_uncorrectable"].candidates[0] == "DCGM_FI_DEV_ECC_DBE_AGG_TOTAL"


def test_source_kind_selects_the_backend():
    assert isinstance(for_source(CENTRAL, 1.0), PrometheusBackend)
    assert isinstance(for_source(CENTRAL, 1.0), Backend)


def test_an_unknown_source_kind_is_unavailable_not_a_crash():
    with pytest.raises(MetricsUnavailable):
        for_source(sources.Source(cluster="us", url="https://x", kind="clickhouse"), 1.0)


def test_device_class_selector_expands_to_vendor_names():
    """The exporter labels by what the vendor calls the card, never by our class name."""
    where = selector(Scope(kind="device_class", match={"device_class": ("rtx-pro-6000-96gb-server-edition",)}))
    assert where == 'modelName="NVIDIA RTX PRO 6000 Blackwell Server Edition"'


def test_device_class_selector_alternates_when_a_class_has_several_names():
    table = DeviceTable(
        [
            DeviceClass(
                name="h100-80gb-sxm",
                label="NVIDIA H100 80GB SXM",
                architecture="hopper",
                vendor="nvidia",
                memory_mib=81559,
                form_factor="sxm",
                aliases={"dcgm": ("NVIDIA H100 80GB HBM3", 'we"ird')},
            )
        ]
    )
    values = table.aliases("h100-80gb-sxm", DIALECT_DCGM)
    assert matcher("modelName", values) == 'modelName=~"NVIDIA H100 80GB HBM3|we\\"ird"'


def test_unmapped_device_class_never_selects_the_whole_fleet(caplog):
    with caplog.at_level("WARNING"):
        where = selector(Scope(kind="device_class", match={"device_class": ("zephyr-4000-ultra",)}))
    assert where == 'modelName="zephyr-4000-ultra"'
    assert "device_class_unmapped" in caplog.text


async def test_candidates_render_as_an_or_chain_for_a_non_sum_metric():
    backend, _ = backend_with({})
    expression = await backend._expression(BY_NAME["inference.kv_cache_utilization"], "", "5m")
    assert expression.count(" or ") == 1
    assert "vllm:gpu_cache_usage_perc" in expression and "vllm:kv_cache_usage_perc" in expression


async def test_a_summed_metric_never_unions_two_populated_candidates():
    """Both spellings live during an upgrade; a union under sum doubles the number."""
    metric = BY_NAME["gpu.ecc_uncorrectable"]
    answers = {
        "count(DCGM_FI_DEV_ECC_DBE_AGG_TOTAL)": sample("4"),
        "count(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL)": sample("4"),
        "sum(DCGM_FI_DEV_ECC_DBE_AGG_TOTAL)": sample("7"),
        "sum(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL)": sample("7"),
    }
    backend, _ = backend_with(answers)
    expression = await backend._expression(metric, "", "5m")
    assert expression == "sum(DCGM_FI_DEV_ECC_DBE_AGG_TOTAL)"
    assert " or " not in expression

    values = await backend.samples(Request(scope=Scope(kind="cluster"), metrics=(metric,), window=Window("5m")))
    assert values["gpu.ecc_uncorrectable"] == 7.0


async def test_a_summed_metric_falls_back_to_the_populated_candidate():
    metric = BY_NAME["gpu.ecc_uncorrectable"]
    backend, _ = backend_with(
        {
            "count(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL)": sample("4"),
            "sum(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL)": sample("3"),
        }
    )
    values = await backend.samples(Request(scope=Scope(kind="cluster"), metrics=(metric,), window=Window("5m")))
    assert values["gpu.ecc_uncorrectable"] == 3.0


async def test_no_candidate_populated_is_none_not_zero():
    metric = BY_NAME["gpu.ecc_uncorrectable"]
    backend, _ = backend_with({})
    values = await backend.samples(Request(scope=Scope(kind="cluster"), metrics=(metric,), window=Window("5m")))
    assert values["gpu.ecc_uncorrectable"] is None


async def test_nan_is_no_series_not_zero():
    """An empty histogram quantiles to NaN, and NaN drawn as 0 reads as an idle GPU."""
    metric = BY_NAME["inference.ttft"]
    expression = render(metric, DIALECT["inference.ttft"].candidates[0], "", "5m")
    backend, _ = backend_with({expression: [{"metric": {}, "value": [0.0, "NaN"]}]})
    values = await backend.samples(Request(scope=Scope(kind="deployment"), metrics=(metric,), window=Window("5m")))
    assert values["inference.ttft"] is None


async def test_a_backend_only_answers_the_metrics_it_has_a_dialect_for():
    """Room for a cost family answered out of inventory rather than a store."""
    backend, _ = backend_with({})
    assert backend.answers(BY_NAME["gpu.utilization"])
    assert not backend.answers(Metric("cost.hourly", "count", "gpu", "Cost per hour"))


def test_answer_distinguishes_absent_from_zero():
    """No series is not an idle GPU, so the value is nullable and the reason travels."""
    answer = MetricsSchema(scope="cluster", ref="clu_1", cluster="clu_1", source=None, window="5m")
    assert answer.samples == []
    assert answer.unavailable is None
    assert SampleSchema(metric="gpu.utilization", title="t", unit="percent", value=None).value is None
    assert "unavailable" in MetricsSchema.__struct_fields__


class FakeRecipe:
    def __init__(self, name: str, engine_args: dict | None = None) -> None:
        self.name = name
        self.engine_args = engine_args or {}


class FakeArtifact:
    def __init__(self, name: str) -> None:
        self.name = name


def test_served_model_prefers_the_operators_explicit_name():
    recipe = FakeRecipe("gemma-4-31b-it", {"served_model_name": "gemma-canary"})
    assert served_model(recipe, FakeArtifact("google/gemma-4-31B-it")) == "gemma-canary"


def test_served_model_falls_back_to_the_recipe_before_the_artifact():
    """The artifact is a repository path and the engine serves under the bare name.

    Reaching for `google/gemma-4-31B-it` built `model_name="google/gemma-4-31B-it"`,
    which matched no series in the fleet's store and emptied every serving panel
    while the data was sitting there under `gemma-4-31b-it`.
    """
    recipe = FakeRecipe("gemma-4-31b-it")
    assert served_model(recipe, FakeArtifact("google/gemma-4-31B-it")) == "gemma-4-31b-it"


def test_served_model_uses_the_artifact_only_when_nothing_else_names_it():
    assert served_model(FakeRecipe(""), FakeArtifact("Qwen/Qwen3.8-27B")) == "Qwen/Qwen3.8-27B"


DYNAMO_LABELS = {
    "dynamo-component": "VllmDecodeWorker",
    "dynamo-namespace": "dynamo",
    "dynamo-worker-hash": "891b8dda",
    "dynamo-graph-deployment-name": "gemma-4-31b-it",
}


def test_worker_identity_composes_the_namespace_dynamo_reports_under():
    assert worker_identity(DYNAMO_LABELS) == "dynamo-891b8dda"


def test_worker_identity_keeps_a_graphs_own_namespace():
    """One deployment in the fleet sets `DYN_NAMESPACE` on its graph, so the hash is not enough.

    `dynamo-<hash>` and `dynamo-<graph>-<hash>` both occur, which is why the
    value is read from the labels rather than rebuilt from the deployment name.
    """
    labels = {"dynamo-namespace": "dynamo-qwen3-8-27b-agg", "dynamo-worker-hash": "b16ef7d1"}
    assert worker_identity(labels) == "dynamo-qwen3-8-27b-agg-b16ef7d1"


def test_worker_identity_is_empty_when_either_half_is_missing():
    """Empty means "not observed yet", which must fall back rather than select nothing."""
    assert worker_identity({"dynamo-namespace": "dynamo"}) == ""
    assert worker_identity({"dynamo-worker-hash": "891b8dda"}) == ""
    assert worker_identity({}) == ""


def test_the_worker_dimension_selects_on_the_dynamo_namespace():
    scope = Scope(kind="deployment", cluster="cluster-ap", match={"worker": ("dynamo-891b8dda",)})
    assert selector(scope) == 'cluster="cluster-ap",dynamo_namespace="dynamo-891b8dda"'


def test_the_worker_join_separates_two_deployments_of_one_model():
    """The whole point: `model_name` cannot tell them apart and `dynamo_namespace` can."""
    one = Scope(kind="deployment", cluster="c", match={"worker": ("dynamo-aaaa",)})
    two = Scope(kind="deployment", cluster="c", match={"worker": ("dynamo-bbbb",)})
    assert selector(one) != selector(two)
