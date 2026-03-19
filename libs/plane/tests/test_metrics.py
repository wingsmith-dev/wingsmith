from app.modules.metrics import sources
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.routes import MetricsRoutes
from app.modules.metrics.schemas import SampleSchema
from app.modules.metrics.schemas import MetricsSchema
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.registry import ENGINES
from app.modules.metrics.registry import SCOPE_LABELS
from app.modules.metrics.registry import family
from app.modules.metrics.services import MetricsService
from app.modules.metrics.services import _escape
from app.modules.metrics.services import _render
from app.modules.metrics.services import _selector


WRITE_METHODS = ("create", "update", "delete", "patch", "post", "add", "remove")


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


def test_sources_are_parsed_per_cluster():
    parsed = sources.parse("us=https://prom.us:9090/,ap=https://prom.ap:9090|kind=prometheus")
    assert set(parsed) == {"us", "ap"}
    assert parsed["us"].url == "https://prom.us:9090"
    assert parsed["ap"].kind == "prometheus"


def test_malformed_source_entries_are_dropped_not_raised():
    assert sources.parse("nonsense,us=,=https://x,us=https://prom:9090") == {
        "us": sources.Source(cluster="us", url="https://prom:9090")
    }


def test_selector_quotes_are_escaped():
    assert _escape('a"b') == 'a\\"b'
    assert _selector("Hostname", ['no"pe']) == 'Hostname="no\\"pe"'


def test_selector_switches_to_regex_for_many_values():
    assert _selector("Hostname", ["a"]) == 'Hostname="a"'
    assert _selector("Hostname", ["a", "b"]) == 'Hostname=~"a|b"'
    assert _selector("Hostname", []) == ""


def test_bare_series_metrics_are_aggregated_and_placeholders_are_substituted():
    util = BY_NAME["gpu.utilization"]
    assert _render(util, 'Hostname="n1"', "5m") == 'avg(DCGM_FI_DEV_GPU_UTIL{Hostname="n1"})'
    assert _render(util, "", "5m", by="UUID") == "avg by (UUID) (DCGM_FI_DEV_GPU_UTIL)"

    ttft = BY_NAME["inference.ttft"]
    rendered = _render(ttft, 'model_name="q"', "5m")
    assert "$SELECTOR" not in rendered and "$WINDOW" not in rendered
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


def test_engines_declare_how_their_series_are_keyed():
    assert ENGINES["vllm"].identity_label == "model_name"
    assert SCOPE_LABELS["node"] == "Hostname"
    assert SCOPE_LABELS["device"] == "UUID"


def test_metric_names_are_unique_across_dialects():
    names = [m.name for engine in ENGINES.values() for m in engine.metrics]
    assert len(names) == len(set(names))


def test_answer_distinguishes_absent_from_zero():
    """No series is not an idle GPU, so the value is nullable and the reason travels."""
    answer = MetricsSchema(scope="cluster", ref="clu_1", cluster="clu_1", source=None, window="5m")
    assert answer.samples == []
    assert answer.unavailable is None
    assert SampleSchema(metric="gpu.utilization", title="t", unit="percent", value=None).value is None
    assert "unavailable" in MetricsSchema.__struct_fields__
