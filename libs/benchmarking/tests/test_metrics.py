"""The field maps, read against one export per harness.

Two kinds of fixture. The `captured` ones are real AIPerf 0.12.0 exports, recorded
against `fixtures/failing_stub.py` with only the local artifact directory and port
rewritten: one run of 20 requests in which every fourth failed, and one in which
nothing did. They pin every AIPerf path this package reads, and the one fact a
hand-written file can only assume: that `error_request_count` is absent, not zero,
on a clean run.

The `with-failures` ones are written by hand, one per harness, each a run of 100
requests in which 5 failed with the same latencies, so a test can ask whether the
four harnesses read the same run as the same numbers. The AIPerf one spreads its 5
failures over two error types, which is the case that tells a request count from a
count of kinds.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from wingsbench import metrics


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


def test_a_captured_aiperf_export_counts_failed_requests() -> None:
    summary = _load("aiperf-0.12.0-captured-with-failures.json")
    assert summary["aiperf_version"] == "0.12.0"
    assert summary["error_request_count"] == {"unit": "requests", "avg": 5.0}

    readings = metrics.read("aiperf", "0.12.0", summary)

    assert {field_id: r.missing for field_id, r in readings.items() if r.missing} == {}
    assert readings[metrics.ERROR_COUNT].value == 5.0
    assert readings[metrics.REQUEST_COUNT].value == 15.0


def test_a_captured_clean_aiperf_export_reads_zero_failures() -> None:
    summary = _load("aiperf-0.12.0-captured-clean.json")
    assert "error_request_count" not in summary

    readings = metrics.read("aiperf", "0.12.0", summary)

    assert {field_id: r.missing for field_id, r in readings.items() if r.missing} == {}
    assert readings[metrics.ERROR_COUNT].value == 0.0
    assert readings[metrics.REQUEST_COUNT].value == 20.0


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
