"""The field maps, read against one export per harness.

Each fixture is a run of 100 requests in which 5 failed, shaped the way that harness
writes its export. The AIPerf one spreads its 5 failures over two error types, which
is the case that tells a request count from a count of kinds.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from hgrbench import metrics


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _readings(harness: str, version: str, summary: dict) -> dict[str, float | None]:
    return {field_id: reading.value for field_id, reading in metrics.read(harness, version, summary).items()}


def test_aiperf_errors_are_failed_requests_not_error_types() -> None:
    summary = _load("aiperf-0.12.0-with-failures.json")
    assert len(summary["error_summary"]) == 2

    readings = _readings("aiperf", "0.12.0", summary)

    assert readings[metrics.ERROR_COUNT] == 5.0
    assert readings[metrics.REQUEST_COUNT] == 95.0


def test_aiperf_omits_the_error_count_when_nothing_failed() -> None:
    summary = _load("aiperf-0.12.0-with-failures.json")
    del summary["error_request_count"]
    summary["error_summary"] = []

    reading = metrics.read("aiperf", "0.12.0", summary)[metrics.ERROR_COUNT]

    assert reading.value == 0.0
    assert reading.missing is None


def test_an_empty_export_is_a_miss_not_zero_failures() -> None:
    reading = metrics.read("aiperf", "0.12.0", {})[metrics.ERROR_COUNT]

    assert reading.value is None
    assert reading.missing == "error_request_count.avg"


@pytest.mark.parametrize(
    ("harness", "version", "fixture"),
    [
        ("aiperf", "0.12.0", "aiperf-0.12.0-with-failures.json"),
        ("vllm-bench", "0.28.0", "vllm-bench-0.28.0-with-failures.json"),
        ("vllm-sweep", "0.28.0", "vllm-bench-0.28.0-with-failures.json"),
        ("guidellm", "0.7.3", "guidellm-0.7.3-with-failures.json"),
    ],
)
def test_every_harness_counts_the_same_five_failures(harness: str, version: str, fixture: str) -> None:
    summary = _load(fixture)
    if harness == "guidellm":
        summary = summary["benchmarks"][0]

    readings = _readings(harness, version, summary)

    assert readings[metrics.ERROR_COUNT] == 5.0
    assert readings[metrics.REQUEST_COUNT] == 95.0
    assert readings[metrics.TTFT_P90] == pytest.approx(620.0)
    assert readings[metrics.E2E_P90] == pytest.approx(6400.0)


def test_guidellm_latency_is_read_from_the_successful_split_in_milliseconds() -> None:
    summary = _load("guidellm-0.7.3-with-failures.json")["benchmarks"][0]

    readings = _readings("guidellm", "0.7.3", summary)

    assert readings[metrics.E2E_AVG] == pytest.approx(5900.0)


def test_a_version_outside_every_range_reads_nothing() -> None:
    summary = _load("guidellm-0.7.3-with-failures.json")["benchmarks"][0]

    assert metrics.read("guidellm", "0.8.1", summary) == {}
    assert metrics.read("guidellm", "unknown", summary) == {}
