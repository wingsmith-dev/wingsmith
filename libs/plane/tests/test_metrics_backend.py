"""The Prometheus backend and its transport, against a stubbed store.

`test_metrics.py` covers the vocabulary — sources, selectors, the PromQL the
renderer produces. This covers what happens to the numbers afterwards: which
expressions are actually issued, how a store's answer becomes a sample, and
what a store that is down looks like from the outside.
"""

import json
import math
import urllib.error

from unittest.mock import patch

import pytest

from app.modules.metrics import sources
from app.modules.metrics.client import Client
from app.modules.metrics.client import MetricsUnavailable
from app.modules.metrics.backend import Scope
from app.modules.metrics.backend import Window
from app.modules.metrics.backend import Request
from app.modules.metrics.registry import BY_NAME
from app.modules.metrics.prometheus import PrometheusBackend


SOURCE = sources.Source(cluster="us", url="https://prom", label="cluster-us")

UTILIZATION = BY_NAME["gpu.utilization"]
POWER = BY_NAME["gpu.power"]
TEMPERATURE = BY_NAME["gpu.temperature"]
ECC = BY_NAME["gpu.ecc_uncorrectable"]


class StubClient:
    """Stands in for the transport, recording what was asked and replaying answers.

    `answers` maps a substring of the expression onto the result to return, so a
    test says what a series holds without restating the PromQL the renderer
    produced — which is `test_metrics.py`'s business, not this file's.
    """

    def __init__(self, answers=None, ranges=None):
        self.answers = answers or {}
        self.ranges = ranges or {}
        self.instant_queries: list[str] = []
        self.range_queries: list[tuple[str, float, float, str]] = []

    async def instant(self, query):
        self.instant_queries.append(query)
        for needle, result in self.answers.items():
            if needle in query:
                return result
        return []

    async def range(self, query, start, end, step):
        self.range_queries.append((query, start, end, step))
        for needle, result in self.ranges.items():
            if needle in query:
                return result
        return []


def backend(client) -> PrometheusBackend:
    b = PrometheusBackend(SOURCE, timeout=5.0)
    b.client = client
    return b


def sample(value, labels=None):
    return {"metric": labels or {}, "value": [1787158053.0, str(value)]}


def scope(**kw) -> Scope:
    return Scope(kind=kw.pop("kind", "cluster"), cluster=kw.pop("cluster", "cluster-us"), **kw)


def request(*metrics, window="5m", **kw) -> Request:
    return Request(scope=scope(**kw), metrics=metrics, window=Window(lookback=window))


# --- samples ---------------------------------------------------------------


async def test_a_sample_comes_back_as_a_number():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample(72.5)]})
    got = await backend(client).samples(request(UTILIZATION))
    assert got == {"gpu.utilization": 72.5}


async def test_a_series_with_nothing_in_it_is_none_and_never_zero():
    """A zero reads as an idle GPU, which is the one wrong answer that looks right."""
    got = await backend(StubClient()).samples(request(UTILIZATION))
    assert got == {"gpu.utilization": None}


async def test_a_nan_is_none_and_never_zero():
    """Prometheus answers a quantile over an empty histogram with NaN."""
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample("NaN")]})
    got = await backend(client).samples(request(UTILIZATION))
    assert got["gpu.utilization"] is None


async def test_an_unparseable_value_is_none():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [{"metric": {}, "value": [1.0, "n/a"]}]})
    assert (await backend(client).samples(request(UTILIZATION)))["gpu.utilization"] is None


async def test_a_malformed_sample_is_none_rather_than_an_error():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [{"metric": {}}]})
    assert (await backend(client).samples(request(UTILIZATION)))["gpu.utilization"] is None


async def test_an_infinity_survives_because_it_is_a_real_answer():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample("+Inf")]})
    got = (await backend(client).samples(request(UTILIZATION)))["gpu.utilization"]
    assert got is not None and math.isinf(got)


async def test_a_metric_this_backend_does_not_answer_is_none_not_an_error():
    """`answers()` is how a backend says which metrics are its own."""
    b = backend(StubClient())
    fake = type(UTILIZATION)(name="cost.hourly", unit="count", family="gpu", title="Cost")
    assert not b.answers(fake)
    assert (await b.samples(request(fake)))["cost.hourly"] is None


async def test_every_requested_metric_is_answered_for():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample(50)]})
    got = await backend(client).samples(request(UTILIZATION, POWER, TEMPERATURE))
    assert set(got) == {"gpu.utilization", "gpu.power", "gpu.temperature"}
    assert got["gpu.power"] is None


async def test_the_scope_reaches_the_expression():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample(50)]})
    await backend(client).samples(request(UTILIZATION, cluster="cluster-ap"))
    assert any("cluster-ap" in q for q in client.instant_queries)


# --- the double-count guard ------------------------------------------------


async def test_a_summed_metric_with_two_spellings_probes_before_it_queries():
    """A union under a sum would add the same counter twice during an upgrade window."""
    client = StubClient(
        {"count(DCGM_FI_DEV_ECC_DBE_AGG_TOTAL": [sample(4)], "DCGM_FI_DEV_ECC_DBE_AGG_TOTAL{": [sample(11)]}
    )
    got = await backend(client).samples(request(ECC))

    assert got["gpu.ecc_uncorrectable"] == 11
    issued = [q for q in client.instant_queries if not q.startswith("count(")]
    assert len(issued) == 1
    assert " or " not in issued[0], f"a summed metric unioned two candidates: {issued[0]}"
    assert "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL" not in issued[0]


async def test_a_summed_metric_falls_through_to_the_second_spelling():
    client = StubClient(
        {"count(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": [sample(2)], "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{": [sample(7)]}
    )
    got = await backend(client).samples(request(ECC))

    assert got["gpu.ecc_uncorrectable"] == 7
    issued = [q for q in client.instant_queries if not q.startswith("count(")]
    assert " or " not in issued[0]
    assert "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL" in issued[0]


async def test_a_summed_metric_with_neither_spelling_populated_asks_a_well_formed_query():
    """None populated: the last candidate is used so the query returns a clean empty."""
    client = StubClient()
    got = await backend(client).samples(request(ECC))

    assert got["gpu.ecc_uncorrectable"] is None
    issued = [q for q in client.instant_queries if not q.startswith("count(")]
    assert len(issued) == 1 and " or " not in issued[0]


async def test_a_non_summed_metric_unions_its_candidates():
    """`or` is idempotent under an average or a max, so the cheaper path is safe."""
    throttle = BY_NAME["gpu.throttle_reasons"]
    client = StubClient({"DCGM_FI_DEV_CLOCK_THROTTLE_REASONS": [sample(1)]})
    await backend(client).samples(request(throttle))

    issued = [q for q in client.instant_queries if not q.startswith("count(")]
    assert " or " in issued[0], "a max over two spellings should union rather than probe"
    assert not [q for q in client.instant_queries if q.startswith("count(")], (
        "an idempotent aggregate should not pay for a probe"
    )


async def test_a_single_candidate_metric_never_probes():
    client = StubClient({"DCGM_FI_DEV_POWER_USAGE": [sample(410)]})
    await backend(client).samples(request(POWER))
    assert not [q for q in client.instant_queries if q.startswith("count(")]


# --- breakdown -------------------------------------------------------------


async def test_a_breakdown_groups_by_the_dimension_it_was_given():
    client = StubClient(
        {
            "DCGM_FI_DEV_GPU_UTIL": [
                sample(90, {"UUID": "GPU-aaa"}),
                sample(10, {"UUID": "GPU-bbb"}),
            ]
        }
    )
    got = await backend(client).breakdown(request(UTILIZATION, breakdown="device"))
    assert got == {"GPU-aaa": {"gpu.utilization": 90.0}, "GPU-bbb": {"gpu.utilization": 10.0}}


async def test_a_breakdown_on_no_dimension_is_empty_rather_than_the_whole_fleet():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample(90, {"UUID": "GPU-aaa"})]})
    assert await backend(client).breakdown(request(UTILIZATION)) == {}
    assert not client.instant_queries, "an unbreakdownable request still queried the store"


async def test_a_breakdown_drops_a_series_with_no_key():
    client = StubClient({"DCGM_FI_DEV_GPU_UTIL": [sample(90, {"UUID": "GPU-aaa"}), sample(5, {})]})
    got = await backend(client).breakdown(request(UTILIZATION, breakdown="device"))
    assert list(got) == ["GPU-aaa"]


async def test_a_breakdown_collects_every_metric_under_one_key():
    client = StubClient(
        {
            "DCGM_FI_DEV_GPU_UTIL": [sample(90, {"UUID": "GPU-aaa"})],
            "DCGM_FI_DEV_POWER_USAGE": [sample(410, {"UUID": "GPU-aaa"})],
        }
    )
    got = await backend(client).breakdown(request(UTILIZATION, POWER, breakdown="device"))
    assert got["GPU-aaa"] == {"gpu.utilization": 90.0, "gpu.power": 410.0}


# --- curves ----------------------------------------------------------------


async def test_a_curve_carries_its_points_and_labels():
    client = StubClient(
        ranges={
            "DCGM_FI_DEV_GPU_UTIL": [
                {
                    "metric": {"__name__": "DCGM_FI_DEV_GPU_UTIL", "UUID": "GPU-aaa"},
                    "values": [[1787158053, "10"], [1787158113, "20"]],
                }
            ]
        }
    )
    b = backend(client)
    got = await b.curves(
        Request(scope=scope(), metrics=(UTILIZATION,), window=Window(lookback="5m", step="60s", span=3600))
    )

    assert len(got) == 1
    curve = got[0]
    assert curve.metric == "gpu.utilization"
    assert curve.points == ((1787158053.0, 10.0), (1787158113.0, 20.0))
    assert curve.labels == {"UUID": "GPU-aaa"}, "__name__ is the series' spelling, not a Hangar label"


async def test_a_curve_skips_a_point_it_cannot_read_rather_than_dropping_the_series():
    client = StubClient(
        ranges={
            "DCGM_FI_DEV_GPU_UTIL": [
                {
                    "metric": {},
                    "values": [[1787158053, "10"], [1787158113, "NaN-ish"], [1787158173, "30"]],
                }
            ]
        }
    )
    b = backend(client)
    got = await b.curves(
        Request(scope=scope(), metrics=(UTILIZATION,), window=Window(lookback="5m", step="60s", span=3600))
    )
    assert got[0].points == ((1787158053.0, 10.0), (1787158173.0, 30.0))


async def test_a_curve_request_spans_the_window_it_was_given():
    client = StubClient(ranges={"DCGM_FI_DEV_GPU_UTIL": []})
    b = backend(client)
    await b.curves(Request(scope=scope(), metrics=(UTILIZATION,), window=Window(lookback="5m", step="30s", span=1800)))

    (_, start, end, step) = client.range_queries[0]
    assert step == "30s"
    assert end - start == pytest.approx(1800, abs=1)


async def test_a_metric_this_backend_cannot_answer_produces_no_curve():
    b = backend(StubClient())
    fake = type(UTILIZATION)(name="cost.hourly", unit="count", family="gpu", title="Cost")
    got = await b.curves(Request(scope=scope(), metrics=(fake,), window=Window(lookback="5m", step="60s", span=3600)))
    assert got == []


# --- the transport ---------------------------------------------------------


def _urlopen_returning(payload, capture=None):
    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def opener(request, timeout=None):
        if capture is not None:
            capture.append((request, timeout))
        return Response()

    return opener


async def test_the_client_lifts_the_result_out_of_a_successful_answer():
    payload = {"status": "success", "data": {"result": [sample(1)]}}
    with patch("urllib.request.urlopen", _urlopen_returning(payload)):
        got = await Client(SOURCE, 5.0).instant("up")
    assert got == [sample(1)]


async def test_the_client_sends_its_token_when_it_has_one():
    captured = []
    authed = sources.Source(cluster="us", url="https://prom", label="l", token="secret")
    payload = {"status": "success", "data": {"result": []}}
    with patch("urllib.request.urlopen", _urlopen_returning(payload, captured)):
        await Client(authed, 5.0).instant("up")

    request, timeout = captured[0]
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 5.0
    assert "query=up" in request.full_url


async def test_the_client_sends_no_authorization_header_without_a_token():
    captured = []
    payload = {"status": "success", "data": {"result": []}}
    with patch("urllib.request.urlopen", _urlopen_returning(payload, captured)):
        await Client(SOURCE, 5.0).instant("up")
    assert captured[0][0].get_header("Authorization") is None


async def test_a_range_query_sends_its_bounds_and_step():
    captured = []
    payload = {"status": "success", "data": {"result": []}}
    with patch("urllib.request.urlopen", _urlopen_returning(payload, captured)):
        await Client(SOURCE, 5.0).range("up", 1000.0, 2000.0, "60s")

    url = captured[0][0].full_url
    assert "query_range" in url
    assert "start=1000.000" in url and "end=2000.000" in url and "step=60s" in url


async def test_a_store_that_is_down_is_a_503_and_never_a_500():
    """A metrics store being down is not the plane's failure: the inventory answer still stands."""

    def boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", boom), pytest.raises(MetricsUnavailable) as caught:
        await Client(SOURCE, 5.0).instant("up")

    assert caught.value.status == 503
    assert caught.value.code == "metrics_unavailable"
    assert "us" in str(caught.value)


async def test_an_http_error_carries_its_status_through():
    def boom(request, timeout=None):
        raise urllib.error.HTTPError("https://prom", 401, "Unauthorized", {}, None)

    with patch("urllib.request.urlopen", boom), pytest.raises(MetricsUnavailable) as caught:
        await Client(SOURCE, 5.0).instant("up")
    assert "401" in str(caught.value)


async def test_a_timeout_is_metrics_unavailable_rather_than_an_unhandled_error():
    def boom(request, timeout=None):
        raise TimeoutError("timed out")

    with patch("urllib.request.urlopen", boom), pytest.raises(MetricsUnavailable):
        await Client(SOURCE, 5.0).instant("up")


async def test_something_that_is_not_json_is_metrics_unavailable():
    class Response:
        def read(self):
            return b"<html>502 Bad Gateway</html>"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    with patch("urllib.request.urlopen", lambda *a, **k: Response()), pytest.raises(MetricsUnavailable):
        await Client(SOURCE, 5.0).instant("up")


async def test_a_query_the_store_refuses_carries_its_reason():
    payload = {"status": "error", "error": "parse error at char 3"}
    with patch("urllib.request.urlopen", _urlopen_returning(payload)), pytest.raises(MetricsUnavailable) as caught:
        await Client(SOURCE, 5.0).instant("up{{")
    assert "parse error" in str(caught.value)


async def test_a_successful_answer_with_no_data_is_an_empty_result_not_an_error():
    with patch("urllib.request.urlopen", _urlopen_returning({"status": "success"})):
        assert await Client(SOURCE, 5.0).instant("up") == []
