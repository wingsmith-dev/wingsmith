"""A ramp's runs read back as a load curve, and the knee derived off them.

One frozen table and two pure functions, so none of this needs a database: a knee is
`f(points, criteria)` by construction, and if it ever stops being testable this way it
has stopped being derivable at read.

The last group here is the one that matters most and looks least like it: the
display half of the field map exists so that a number a chart must draw cannot
reach a verdict, and the guard is structural rather than remembered.
"""

from types import SimpleNamespace

import pytest

from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.modules.benchmarks import curves
from app.modules.benchmarks import verdicts
from app.modules.benchmarks import summaries
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.benchmarks.services import STEPPED
from app.modules.benchmarks.services import BenchmarkService


CRITERIA = verdicts.ACCEPTANCE_2026_1
DIGEST = "svg_" + "a" * 64
CEILING = 5_000.0
"""The `lt6k` P90 ceiling the 2026.1 standard sets, which every ramp here sits in."""


def aiperf(p90=None, p50=None, total=None, per_stream=None, p95=None):
    """Only the keys asked for, so a path this leaves out is a real missing path."""
    summary: dict = {}
    ttft = {key: value for key, value in (("p90", p90), ("p50", p50), ("p95", p95)) if value is not None}
    if ttft:
        summary["time_to_first_token"] = ttft
    if total is not None:
        summary["output_token_throughput"] = {"avg": total}
    if per_stream is not None:
        summary["output_token_throughput_per_user"] = {"avg": per_stream}
    return summary or None


def run(uid, step, load, p90=None, phase="succeeded", discard=False, version="0.12.0", tokens=2000, digest=DIGEST, **summary):
    return SimpleNamespace(
        uid=uid,
        step_index=step,
        harness="aiperf",
        harness_version=version,
        parameters={"concurrency": load, "input_tokens_mean": tokens},
        phase=phase,
        discard=discard,
        serving_digest=digest,
        summary=aiperf(p90=p90, **summary),
    )


def ramp(*pairs, **kwargs):
    """One run per `(load, p90)` pair, stepped in the order they are given."""
    return curves.points(
        [run(f"bmk_{index}", index, load, p90=p90, **kwargs) for index, (load, p90) in enumerate(pairs)],
        curves.CONCURRENCY,
    )


# ---------------------------------------------------------------- points


def test_the_step_field_a_mode_walks_is_the_one_the_expansion_created():
    """Two copies of the table on purpose; a shared constant would follow an edit silently."""
    assert curves.STEP_FIELDS == STEPPED


def test_a_curve_is_listed_by_step_index_whatever_order_the_rows_arrive_in():
    points = curves.points(
        [run("bmk_2", 2, 64, p90=900.0), run("bmk_0", 0, 16, p90=300.0), run("bmk_1", 1, 4, p90=200.0)],
        curves.CONCURRENCY,
    )
    assert [point.step_index for point in points] == [0, 1, 2]
    assert [point.step_value for point in points] == [16, 4, 64]


def test_every_reading_the_contract_names_is_on_every_point():
    """The nine ids spelled out, because the console keys off them by name."""
    point = ramp((8, 300.0))[0]
    assert set(point.readings) == {
        "ttft_p50_ms",
        "ttft_p90_ms",
        "ttft_p95_ms",
        "ttft_p99_ms",
        "itl_p50_ms",
        "e2e_p95_ms",
        "output_tps_per_stream",
        "output_tps_total",
        "request_throughput",
    }


def test_a_reading_that_did_not_resolve_is_named_rather_than_read_as_zero():
    point = ramp((8, 300.0))[0]
    absent = point.readings["output_tps_total"]
    assert absent.value is None
    assert absent.path == "output_token_throughput.avg"
    assert absent.unit == summaries.TOKENS_PER_SECOND


def test_a_discarded_warm_up_stays_on_the_curve_and_out_of_the_knee():
    points = curves.points(
        [
            run("bmk_0", 0, 1, p90=100.0, discard=True),
            run("bmk_1", 1, 8, p90=200.0),
            run("bmk_2", 2, 16, p90=9000.0),
        ],
        curves.CONCURRENCY,
    )
    assert [point.discard for point in points] == [True, False, False]
    knee = curves.knee(points, CRITERIA)
    assert knee.found is True
    assert knee.step_value == 16


def test_a_run_still_open_is_a_point_with_nothing_read_off_it():
    """The console draws a partial curve while the ramp is still climbing."""
    points = curves.points([run("bmk_0", 0, 8, p90=300.0, phase="running")], curves.CONCURRENCY)
    assert points[0].phase == "running"
    assert all(reading.value is None for reading in points[0].readings.values())


def test_a_run_that_failed_is_not_drawn_from_its_partial_summary():
    """A failed harness reports the fraction of the load it got through; a partial summary is a dip that never happened."""
    points = curves.points([run("bmk_0", 0, 8, p90=300.0, phase="failed")], curves.CONCURRENCY)
    assert points[0].readings["ttft_p90_ms"].value is None


def test_a_run_whose_harness_version_has_no_field_map_reads_nothing_and_claims_no_path():
    point = curves.points([run("bmk_0", 0, 8, p90=300.0, version="0.3.0")], curves.CONCURRENCY)[0]
    assert point.readings["ttft_p90_ms"] == curves.Value(None, "", "")


def test_a_run_that_declared_no_load_has_a_null_step_value_rather_than_a_zero():
    bare = SimpleNamespace(
        uid="bmk_0",
        step_index=0,
        harness="aiperf",
        harness_version="0.12.0",
        parameters={},
        phase="succeeded",
        discard=False,
        summary=aiperf(p90=300.0),
    )
    assert curves.points([bare], curves.CONCURRENCY)[0].step_value is None


def test_an_empty_request_is_an_empty_curve():
    assert curves.points([], curves.CONCURRENCY) == []


# ---------------------------------------------------------------- the knee


def test_the_knee_is_the_step_that_crossed_the_bands_ceiling():
    knee = curves.knee(ramp((8, 300.0), (16, 900.0), (32, 2100.0), (64, 6400.0)), CRITERIA)
    assert knee.found is True
    assert knee.method == curves.FIRST_CROSSING
    assert knee.metric == "ttft_p90_ms"
    assert knee.threshold == CEILING
    assert (knee.step_index, knee.step_value) == (3, 64)
    assert "between concurrency 32 and concurrency 64" in knee.reason


def test_the_knee_is_read_along_the_load_and_not_along_the_order_the_steps_were_asked_for():
    """`_steps` preserves the caller's order, so step index is not load order."""
    points = curves.points(
        [run("bmk_0", 0, 16, p90=900.0), run("bmk_1", 1, 4, p90=200.0), run("bmk_2", 2, 64, p90=6400.0)],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.step_value == 64
    assert "between concurrency 16 and concurrency 64" in knee.reason


def test_a_ramp_that_never_crossed_says_so_rather_than_naming_its_top_step():
    knee = curves.knee(ramp((8, 300.0), (16, 600.0), (32, 1200.0), (64, 2400.0)), CRITERIA)
    assert knee.found is False
    assert (knee.step_index, knee.step_value) == (None, None)
    assert knee.threshold == CEILING
    assert "every step stayed under" in knee.reason
    assert "concurrency 64" in knee.reason


def test_a_ramp_that_started_above_the_ceiling_is_a_different_answer_from_one_that_never_crossed():
    knee = curves.knee(ramp((8, 6000.0), (16, 7000.0), (32, 8000.0)), CRITERIA)
    assert knee.found is False
    assert "already over" in knee.reason
    assert "the ramp started above the ceiling" in knee.reason


def test_a_single_point_curve_has_no_crossing_to_find():
    knee = curves.knee(ramp((8, 300.0)), CRITERIA)
    assert knee.found is False
    assert "a crossing needs at least two" in knee.reason


def test_an_empty_curve_says_nothing_succeeded_rather_than_nothing_at_all():
    knee = curves.knee([], CRITERIA)
    assert knee.found is False
    assert knee.reason.startswith("0 of the request's runs succeeded")


def test_a_curve_whose_only_readable_steps_were_discarded_cannot_be_read():
    knee = curves.knee(ramp((8, 300.0), (16, 9000.0), discard=True), CRITERIA)
    assert knee.found is False
    assert "a crossing needs at least two" in knee.reason


def test_a_missing_reading_on_the_knee_metric_stops_the_walk_rather_than_skipping_the_step():
    points = curves.points(
        [run("bmk_0", 0, 8, p90=300.0), run("bmk_1", 1, 16), run("bmk_2", 2, 32, p90=9000.0)],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert "the run at concurrency 16 reported no time_to_first_token.p90" in knee.reason


def test_a_curve_that_crosses_back_under_is_refused_rather_than_read_confidently():
    knee = curves.knee(ramp((8, 300.0), (16, 6000.0), (32, 900.0), (64, 7000.0)), CRITERIA)
    assert knee.found is False
    assert "is not monotone" in knee.reason
    assert "concurrency 16" in knee.reason and "concurrency 32" in knee.reason


def test_a_workload_ramp_with_a_run_per_target_is_not_averaged_into_one_curve():
    """Two deployments on two device classes cross at two loads; one curve through both describes no machine."""
    points = curves.points(
        [
            run("bmk_0", 0, 8, p90=300.0),
            run("bmk_1", 0, 8, p90=400.0),
            run("bmk_2", 1, 16, p90=9000.0),
            run("bmk_3", 1, 16, p90=9100.0),
        ],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert "one per placed target" in knee.reason


def test_steps_that_measured_two_serving_configurations_are_not_one_curve():
    """One graded run per step is not enough: the steps have to be the same machine."""
    points = curves.points(
        [
            run("bmk_0", 0, 8, p90=300.0),
            run("bmk_1", 1, 16, p90=9000.0, digest="svg_" + "b" * 64),
        ],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert "2 serving configurations" in knee.reason


def test_steps_that_landed_in_two_bands_have_no_single_ceiling():
    points = curves.points(
        [run("bmk_0", 0, 8, p90=300.0, tokens=2000), run("bmk_1", 1, 16, p90=900.0, tokens=20000)],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert "one ceiling does not cover them" in knee.reason


def test_a_ramp_that_declared_no_prompt_length_cannot_claim_a_ceiling():
    points = curves.points(
        [run("bmk_0", 0, 8, p90=300.0, tokens=None), run("bmk_1", 1, 16, p90=900.0, tokens=None)],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert knee.threshold is None
    assert "no band applies" in knee.reason


def test_the_standard_sets_no_ceiling_above_256k_and_that_is_not_a_knee():
    points = curves.points(
        [run("bmk_0", 0, 8, p90=300.0, tokens=300_000), run("bmk_1", 1, 16, p90=900.0, tokens=300_000)],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.found is False
    assert "sets no time-to-first-token target for the gt256k band" in knee.reason


@pytest.mark.parametrize("band,ceiling", [("lt6k", 5_000.0), ("16k_32k", 8_000.0), ("64k_128k", 35_000.0)])
def test_the_threshold_comes_from_the_pinned_criteria_and_is_never_a_literal_here(band, ceiling):
    tokens = {"lt6k": 2000, "16k_32k": 20_000, "64k_128k": 100_000}[band]
    points = curves.points(
        [
            run("bmk_0", 0, 8, p90=ceiling / 10, tokens=tokens),
            run("bmk_1", 1, 16, p90=ceiling + 1, tokens=tokens),
        ],
        curves.CONCURRENCY,
    )
    knee = curves.knee(points, CRITERIA)
    assert knee.threshold == ceiling
    assert knee.found is True


def test_a_knee_is_answered_against_the_criteria_version_it_is_asked_for():
    """Derived at read against a pinned table, which is what a stored knee cannot be."""
    assert curves.knee(ramp((8, 300.0)), verdicts.resolve_criteria(None)).method == curves.FIRST_CROSSING
    with pytest.raises(Invalid):
        verdicts.resolve_criteria("acceptance-2099.1")


def test_a_knee_is_always_an_object_and_names_its_method_and_metric_even_when_it_found_nothing():
    knee = curves.knee([], CRITERIA)
    assert (knee.method, knee.metric) == (curves.FIRST_CROSSING, curves.KNEE_METRIC)
    assert knee.reason


# ---------------------------------------------------------------- display is not gradeable


def test_the_aggregate_output_rate_is_unreachable_from_the_grading_reader():
    """The whole reason the map has two halves: a check that could reach this could never fail."""
    summary = {"output_token_throughput": {"avg": 8000.0}, "request_throughput": {"avg": 3.16}}
    for field_id in (summaries.OUTPUT_SPEED_TOTAL, summaries.REQUEST_THROUGHPUT):
        graded = summaries.read(summaries.AIPERF_0_4, summary, field_id)
        assert graded.value is None
        assert graded.missing == field_id
        assert summaries.show(summaries.AIPERF_0_4, summary, field_id).value is not None


def test_no_display_field_is_also_a_gradeable_one():
    graded = {field.id for field in summaries.AIPERF_0_4.fields}
    shown = {field.id for field in summaries.AIPERF_0_4.display}
    assert graded & shown == set()
    assert shown == summaries.DISPLAY_ONLY


def test_no_display_id_can_be_named_by_a_check_because_read_cannot_see_one():
    for field_id in summaries.DISPLAY_ONLY:
        for adapter in (summaries.AIPERF_0_4, summaries.CONFORMANCE_0_1, summaries.EVALSCOPE_0_19):
            assert summaries.read(adapter, {"anything": 1}, field_id).value is None


def test_the_per_stream_rate_stays_gradeable_and_the_aggregate_does_not_stand_in_for_it():
    stripped = {"output_token_throughput": {"avg": 8000.0}}
    assert summaries.read(summaries.AIPERF_0_4, stripped, summaries.OUTPUT_SPEED_PER_STREAM).value is None
    assert summaries.show(summaries.AIPERF_0_4, stripped, summaries.OUTPUT_SPEED_PER_STREAM).value is None


def test_a_curve_draws_the_aggregate_rate_that_no_verdict_may_read():
    point = ramp((8, 300.0))[0]
    drawn = curves.points([run("bmk_0", 0, 8, p90=300.0, total=8000.0)], curves.CONCURRENCY)[0]
    assert drawn.readings["output_tps_total"].value == 8000.0
    assert point.readings["output_tps_total"].value is None


# ---------------------------------------------------------------- the route


def test_the_curve_is_a_read_beside_the_verdict_and_never_a_write():
    methods = {name for name, member in vars(BenchmarkRoutes).items() if hasattr(member, "fn")}
    assert "get_request_curve" in methods
    assert "create_request_curve" not in methods


# ---------------------------------------------------------------- the service read


class _StubRepo:
    """The two reads `curve` makes, and nothing else it could reach."""

    def __init__(self, request, runs):
        self._request = request
        self._runs = runs

    async def request(self, ref):
        return (self._request,) if self._request is not None else None

    async def request_runs(self, ref):
        return self._runs


def request_row(mode="ramp", criteria="acceptance-2026.1"):
    return SimpleNamespace(uid="bmr_0", mode=mode, criteria_version=criteria)


def service(request, runs):
    svc = BenchmarkService(db=None)
    svc.repo = _StubRepo(request, runs)
    return svc


async def test_the_curve_read_carries_the_knee_and_every_reading_to_the_wire():
    runs = [run("bmk_0", 0, 8, p90=300.0, total=8000.0), run("bmk_1", 1, 16, p90=9000.0)]
    answer = await service(request_row(), runs).curve("bmr_0")
    assert answer.request == "bmr_0"
    assert answer.step_field == "concurrency"
    assert answer.serving_digest == DIGEST
    assert [point.step_value for point in answer.points] == [8, 16]
    assert answer.points[0].readings["output_tps_total"].value == 8000.0
    assert answer.points[1].readings["output_tps_total"].value is None
    assert answer.points[1].readings["output_tps_total"].path == "output_token_throughput.avg"
    assert answer.knee.found is True
    assert answer.knee.step_value == 16


async def test_the_x_axis_comes_from_the_mode_the_request_was_created_under():
    runs = [run("bmk_0", 0, 8, p90=300.0, tokens=2000), run("bmk_1", 1, 8, p90=900.0, tokens=20000)]
    answer = await service(request_row(mode="sweep"), runs).curve("bmr_0")
    assert answer.step_field == "input_tokens_mean"
    assert [point.step_value for point in answer.points] == [2000, 20000]


async def test_a_curve_through_two_serving_configurations_names_neither():
    runs = [run("bmk_0", 0, 8, p90=300.0), run("bmk_1", 1, 16, p90=900.0, digest="svg_" + "b" * 64)]
    answer = await service(request_row(), runs).curve("bmr_0")
    assert answer.serving_digest is None


async def test_the_criteria_the_curve_was_read_against_is_on_the_answer_and_can_be_overridden():
    runs = [run("bmk_0", 0, 8, p90=300.0), run("bmk_1", 1, 16, p90=9000.0)]
    svc = service(request_row(), runs)
    assert (await svc.curve("bmr_0")).criteria_version == "acceptance-2026.1"
    assert (await svc.curve("bmr_0", "acceptance-2026.1")).knee.threshold == CEILING
    with pytest.raises(Invalid):
        await svc.curve("bmr_0", "acceptance-2099.1")


async def test_a_request_that_does_not_exist_has_no_curve():
    with pytest.raises(NotFound):
        await service(None, []).curve("bmr_missing")
