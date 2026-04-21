"""Harness kinds, the level they grade at, the verdict, and the bands it is read back in.

Three tables and one pure function, so none of this needs a database: a verdict is
`f(summary, harness, harness_version, criteria)` by construction, and if it ever stops
being testable this way it has stopped being derivable at read.
"""

import math

from types import SimpleNamespace

import pytest

from app.core.exceptions import Invalid
from app.modules.benchmarks import buckets
from app.modules.benchmarks import verdicts
from app.modules.benchmarks import harnesses
from app.modules.benchmarks import summaries
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.benchmarks.schemas import BenchmarkRequestCreateRequest
from app.modules.benchmarks.services import MODES
from app.modules.benchmarks.services import STEPPED
from app.modules.benchmarks.services import BenchmarkService
from app.modules.benchmarks.services import _tier as tier_for
from app.modules.benchmarks.services import _steps as steps_for
from app.modules.benchmarks.services import _homogeneous as homogeneous
from app.modules.benchmarks.services import request_phase


CRITERIA = verdicts.ACCEPTANCE_2026_1


def aiperf_summary(p90=1000.0, p50=500.0, avg=600.0, per_stream=140.0, requests=100):
    return {
        "time_to_first_token": {"unit": "ms", "p90": p90, "p50": p50, "avg": avg},
        "output_token_throughput_per_user": {"unit": "tokens/sec", "avg": per_stream},
        "output_token_throughput": {"unit": "tokens/sec", "avg": 8000.0},
        "request_count": requests,
        "error_count": 0,
    }


def facts(**overrides):
    base = {
        "run_id": "bmk_x",
        "harness": "aiperf",
        "harness_version": "0.12.0",
        "level": "deployment",
        "phase": "succeeded",
        "discard": False,
        "summary": aiperf_summary(),
        "input_tokens": 2000,
        "parameter_count": 8_000_000_000,
        "tier": None,
        "strict": True,
        "datasets": (),
        "baselines": (),
        "homogeneous": True,
    }
    base.update(overrides)
    return verdicts.RunFacts(**base)


def check(verdict, check_id):
    return next(entry for entry in verdict.checks if entry.id == check_id)


# ---------------------------------------------------------------- harness kinds


def test_the_two_new_harness_kinds_are_registered_beside_aiperf():
    """New names in the existing table, riding the existing operation and the existing frame."""
    assert set(harnesses.HARNESSES) == {"aiperf", "conformance", "evalscope"}
    assert harnesses.CONFORMANCE.wire == "conformance"
    assert harnesses.EVALSCOPE.wire == "evalscope"


def test_every_harness_declares_the_levels_it_grades_at():
    for harness in harnesses.HARNESSES.values():
        assert harness.levels, harness.name
        assert set(harness.levels) <= set(harnesses.LEVELS), harness.name


def test_a_probe_suite_has_nothing_to_say_about_a_workload():
    """A workload is a recipe, not a public surface; there is no endpoint to probe."""
    with pytest.raises(Invalid):
        harnesses.resolve("conformance", harnesses.WORKLOAD)


def test_a_load_generator_never_runs_at_the_endpoint_level():
    """What a configuration can sustain is a per-deployment claim.

    What the public name actually did under real load is the gateway's series,
    continuously, and not a synthetic run that measured ten minutes last Tuesday.
    """
    with pytest.raises(Invalid):
        harnesses.resolve("aiperf", harnesses.ENDPOINT)
    assert harnesses.resolve("aiperf", harnesses.DEPLOYMENT) is harnesses.AIPERF


def test_the_accuracy_harness_grades_at_every_level():
    for level in harnesses.LEVELS:
        assert harnesses.resolve("evalscope", level) is harnesses.EVALSCOPE


def test_a_level_nothing_declares_is_refused_before_the_harness_is_consulted():
    with pytest.raises(Invalid):
        harnesses.resolve("evalscope", "fleet")


def test_a_knob_that_only_means_something_at_one_level_is_refused_elsewhere():
    """Below the endpoint there is nothing to fail over to."""
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.CONFORMANCE, {"failover_probe": True}, harnesses.DEPLOYMENT)
    resolved = harnesses.normalise(harnesses.CONFORMANCE, {"failover_probe": True}, harnesses.ENDPOINT)
    assert resolved["failover_probe"] is True


def test_a_level_restricted_knob_left_at_its_default_is_not_an_error():
    resolved = harnesses.normalise(harnesses.CONFORMANCE, {"failover_probe": False}, harnesses.DEPLOYMENT)
    assert resolved["failover_probe"] is False


def test_a_decoding_parameter_is_a_float_and_a_form_posted_one_is_coerced():
    resolved = harnesses.normalise(harnesses.EVALSCOPE, {"temperature": "0.7"}, harnesses.WORKLOAD)
    assert resolved["temperature"] == 0.7
    assert resolved["top_p"] == 1.0


@pytest.mark.parametrize("value", ["hot", -0.5, float("inf"), float("nan")])
def test_a_temperature_that_is_not_a_number_is_refused_rather_than_defaulted(value):
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.EVALSCOPE, {"temperature": value}, harnesses.WORKLOAD)


def test_the_datasets_the_standard_names_are_declared_once():
    assert set(harnesses.DATASETS) == {
        "aime2025",
        "aime2026",
        "hle",
        "mmlu_pro",
        "swe_bench_verified",
        "gpqa_diamond",
        "longbench_v2",
        "livecodebench",
        "tau2_bench",
    }


def test_a_misspelled_dataset_is_a_four_hundred_here_and_not_an_empty_results_block():
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.EVALSCOPE, {"datasets": "mmlu-pro"}, harnesses.WORKLOAD)


def test_a_dataset_list_is_normalised_and_deduplicated():
    resolved = harnesses.normalise(harnesses.EVALSCOPE, {"datasets": " MMLU_Pro, hle ,hle"}, harnesses.WORKLOAD)
    assert resolved["datasets"] == "mmlu_pro,hle"


def test_an_empty_dataset_list_is_not_an_error_and_is_not_a_default_set():
    resolved = harnesses.normalise(harnesses.EVALSCOPE, {}, harnesses.WORKLOAD)
    assert resolved["datasets"] == ""


# ---------------------------------------------------------------- the judge


def test_no_judge_is_pinned_anywhere_in_the_harness_table():
    """This plane is the framework in which grading happens and has no opinion about the grader."""
    judges = {
        parameter.name: parameter.default
        for parameter in harnesses.EVALSCOPE.parameters
        if parameter.name.startswith("judge_") and parameter.type is str
    }
    assert judges == {"judge_base_url": "", "judge_model": "", "judge_credential_id": ""}
    assert "judge" not in harnesses.EVALSCOPE.image


def test_a_judge_is_a_url_and_a_model_or_it_is_neither():
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.EVALSCOPE, {"judge_base_url": "http://judge"}, harnesses.ENDPOINT)
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.EVALSCOPE, {"judge_model": "some-grader"}, harnesses.ENDPOINT)


def test_a_credential_reference_names_a_grader_no_base_url_points_at():
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.EVALSCOPE, {"judge_credential_id": "cre_x"}, harnesses.ENDPOINT)


def test_an_external_grader_is_endpoint_level_work_and_is_refused_inside_a_cluster():
    """A cluster-dispatched benchmark job mounts no credential and is built for no egress."""
    judged = {"judge_base_url": "https://judge.example", "judge_model": "grader", "judge_credential_id": "cre_x"}
    for level in (harnesses.DEPLOYMENT, harnesses.WORKLOAD):
        with pytest.raises(Invalid):
            harnesses.normalise(harnesses.EVALSCOPE, judged, level)
    resolved = harnesses.normalise(harnesses.EVALSCOPE, judged, harnesses.ENDPOINT)
    assert resolved["judge_credential_id"] == "cre_x"


def test_a_locally_resolvable_judge_needs_no_credential_and_is_allowed_in_a_cluster():
    resolved = harnesses.normalise(
        harnesses.EVALSCOPE,
        {"judge_base_url": "http://grader.hangar.svc", "judge_model": "grader"},
        harnesses.WORKLOAD,
    )
    assert resolved["judge_credential_id"] == ""


# ---------------------------------------------------------------- criteria


def test_the_criteria_are_a_frozen_table_and_an_unknown_version_is_refused():
    assert verdicts.resolve_criteria(None) is verdicts.ACCEPTANCE_2026_1
    assert verdicts.resolve_criteria("acceptance-2026.1") is verdicts.ACCEPTANCE_2026_1
    with pytest.raises(Invalid):
        verdicts.resolve_criteria("acceptance-2027.1")


def test_the_acceptance_floor_lives_on_the_criteria_and_not_on_the_baseline_row():
    """Two artifacts must not be graded against two silently different standards."""
    assert CRITERIA.baseline_floor == 0.85


def test_every_band_the_standard_names_has_a_target():
    assert {target.band for target in CRITERIA.ttft} == {band.label for band in buckets.BANDS} - {"gt256k"}


# ---------------------------------------------------------------- the 0.85 floor


def accuracy_facts(score, baseline=0.7412, **overrides):
    return facts(
        harness="evalscope",
        harness_version="0.19.0",
        level="workload",
        summary={"results": {"mmlu_pro": {"metric": "accuracy", "score": score, "n": 12032}}},
        datasets=("mmlu_pro",),
        baselines=(verdicts.Baseline("mmlu_pro", "accuracy", baseline, 3.0),),
        **overrides,
    )


def test_a_score_exactly_on_the_floor_passes():
    """The floor is `0.85 x baseline` and it is inclusive; a boundary must land somewhere stated."""
    floor = 0.7412 * CRITERIA.baseline_floor
    verdict = verdicts.verdict(accuracy_facts(floor), CRITERIA)
    assert verdict.outcome == verdicts.PASS
    assert check(verdict, "accuracy.mmlu_pro").threshold == floor


def test_a_score_one_step_below_the_floor_fails():
    floor = 0.7412 * CRITERIA.baseline_floor
    verdict = verdicts.verdict(accuracy_facts(math.nextafter(floor, 0.0)), CRITERIA)
    assert verdict.outcome == verdicts.FAIL
    assert check(verdict, "accuracy.mmlu_pro").outcome == verdicts.FAIL


def test_a_score_one_step_above_the_floor_passes():
    floor = 0.7412 * CRITERIA.baseline_floor
    assert verdicts.verdict(accuracy_facts(math.nextafter(floor, 1.0)), CRITERIA).outcome == verdicts.PASS


def test_a_dataset_with_no_declared_baseline_is_never_a_pass():
    """An undeclared floor is an open question, which is what the baseline table is for."""
    verdict = verdicts.verdict(accuracy_facts(0.9, baseline=0.7412)._replace(baselines=()), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE
    assert check(verdict, "accuracy.mmlu_pro").outcome == verdicts.INCONCLUSIVE


def test_a_dataset_that_was_not_sat_is_not_applicable_rather_than_failed():
    """A partial eval is a partial eval; the request's own list says which exam was sat."""
    verdict = verdicts.verdict(accuracy_facts(0.9)._replace(summary={"results": {}}), CRITERIA)
    assert check(verdict, "accuracy.mmlu_pro").outcome == verdicts.NOT_APPLICABLE
    assert verdict.outcome == verdicts.INCONCLUSIVE


def test_a_score_against_a_different_metric_than_the_baseline_declares_is_inconclusive():
    graded = accuracy_facts(0.9)._replace(
        summary={"results": {"mmlu_pro": {"metric": "exact_match", "score": 0.9}}},
    )
    assert verdicts.verdict(graded, CRITERIA).outcome == verdicts.INCONCLUSIVE


def test_an_accuracy_verdict_over_a_heterogeneous_coverage_is_not_attributable():
    """Two upstreams on two quantizations produce one score belonging to neither."""
    verdict = verdicts.verdict(accuracy_facts(0.9)._replace(homogeneous=False), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE
    assert "artifact" in check(verdict, "accuracy").detail


# ---------------------------------------------------------------- performance


def test_a_run_inside_its_band_passes_all_three_time_to_first_token_checks():
    verdict = verdicts.verdict(facts(input_tokens=2000, summary=aiperf_summary(p90=4000.0)), CRITERIA)
    assert verdict.outcome == verdicts.PASS
    assert check(verdict, summaries.TTFT_P90).threshold == 5000.0


def test_a_run_over_its_band_ceiling_fails():
    verdict = verdicts.verdict(facts(input_tokens=2000, summary=aiperf_summary(p90=5001.0)), CRITERIA)
    assert verdict.outcome == verdicts.FAIL
    assert check(verdict, summaries.TTFT_P90).outcome == verdicts.FAIL


def test_the_band_comes_from_the_declared_prompt_length_and_a_boundary_takes_the_stricter_side():
    tight = verdicts.verdict(facts(input_tokens=6 * 1024, summary=aiperf_summary(p50=2400.0)), CRITERIA)
    loose = verdicts.verdict(facts(input_tokens=6 * 1024 + 1, summary=aiperf_summary(p50=2400.0)), CRITERIA)
    assert check(tight, summaries.TTFT_P50).threshold == 2000.0
    assert check(loose, summaries.TTFT_P50).threshold == 2500.0
    assert tight.outcome == verdicts.FAIL
    assert loose.outcome == verdicts.PASS


def test_a_run_that_declared_no_prompt_length_cannot_claim_a_band():
    verdict = verdicts.verdict(facts(input_tokens=None), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE


def test_the_standard_sets_no_target_above_256k_and_that_is_not_a_pass():
    verdict = verdicts.verdict(facts(input_tokens=300_000), CRITERIA)
    assert check(verdict, "ttft").outcome == verdicts.NOT_APPLICABLE


def test_a_summary_missing_a_path_names_the_path_rather_than_reading_zero():
    verdict = verdicts.verdict(facts(summary={"request_count": 10}), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE
    assert "time_to_first_token.p90" in check(verdict, summaries.TTFT_P90).detail


def test_output_speed_is_read_per_stream_and_never_from_the_aggregate():
    """An aggregate rate clears a per-stream floor under every condition, which is no check at all."""
    field = next(entry for entry in summaries.AIPERF_0_4.fields if entry.id == summaries.OUTPUT_SPEED_PER_STREAM)
    assert field.path == ("output_token_throughput_per_user", "avg")
    stripped = aiperf_summary()
    stripped.pop("output_token_throughput_per_user")
    verdict = verdicts.verdict(facts(summary=stripped), CRITERIA)
    assert check(verdict, summaries.OUTPUT_SPEED_PER_STREAM).outcome == verdicts.INCONCLUSIVE


def test_a_small_model_is_held_to_the_hundred_token_floor():
    verdict = verdicts.verdict(facts(parameter_count=8_000_000_000), CRITERIA)
    assert check(verdict, summaries.OUTPUT_SPEED_PER_STREAM).threshold == 100.0


def test_a_large_model_is_held_to_the_floor_of_the_tier_the_buyer_assigned():
    l1 = verdicts.verdict(facts(parameter_count=70_000_000_000, tier="L1"), CRITERIA)
    l2 = verdicts.verdict(facts(parameter_count=70_000_000_000, tier="L2"), CRITERIA)
    assert check(l1, summaries.OUTPUT_SPEED_PER_STREAM).threshold == 30.0
    assert check(l2, summaries.OUTPUT_SPEED_PER_STREAM).threshold == 10.0


def test_an_unstated_tier_does_not_silently_take_the_lenient_branch():
    verdict = verdicts.verdict(facts(parameter_count=70_000_000_000, tier=None), CRITERIA)
    assert check(verdict, summaries.OUTPUT_SPEED_PER_STREAM).outcome == verdicts.INCONCLUSIVE
    assert verdict.outcome == verdicts.INCONCLUSIVE


def test_an_artifact_with_no_parameter_count_cannot_choose_a_population():
    verdict = verdicts.verdict(facts(parameter_count=None), CRITERIA)
    assert check(verdict, summaries.OUTPUT_SPEED_PER_STREAM).outcome == verdicts.INCONCLUSIVE


# ---------------------------------------------------------------- conformance


def conformance_facts(counts, failures=(), **overrides):
    return facts(
        harness="conformance",
        harness_version="0.1.0",
        level="endpoint",
        summary={"counts": counts, "failures": list(failures)},
        **overrides,
    )


def test_a_clean_probe_suite_passes():
    verdict = verdicts.verdict(conformance_facts({"total": 84, "pass": 84, "fail": 0, "skip": 0, "error": 0}), CRITERIA)
    assert verdict.outcome == verdicts.PASS


def test_a_required_probe_failure_fails_the_run():
    verdict = verdicts.verdict(
        conformance_facts(
            {"total": 84, "pass": 83, "fail": 1, "skip": 0, "error": 0},
            [{"id": "chat.stream.finish_reason", "required": True, "outcome": "fail"}],
        ),
        CRITERIA,
    )
    assert verdict.outcome == verdicts.FAIL
    assert "chat.stream.finish_reason" in check(verdict, "conformance.required").detail


def test_an_advisory_failure_gates_only_when_the_run_was_strict():
    advisory = [{"id": "chat.tools.parallel", "required": False, "outcome": "fail"}]
    counts = {"total": 84, "pass": 83, "fail": 1, "skip": 0, "error": 0}
    assert verdicts.verdict(conformance_facts(counts, advisory, strict=True), CRITERIA).outcome == verdicts.FAIL
    lenient = verdicts.verdict(conformance_facts(counts, advisory, strict=False), CRITERIA)
    assert lenient.outcome == verdicts.PASS
    assert check(lenient, "conformance.advisory").outcome == verdicts.NOT_APPLICABLE


def test_a_summary_that_counts_failures_but_enumerates_none_contradicted_itself():
    verdict = verdicts.verdict(conformance_facts({"total": 84, "pass": 80, "fail": 4, "skip": 0, "error": 0}), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE


def test_a_probe_that_errored_asserted_nothing():
    verdict = verdicts.verdict(conformance_facts({"total": 84, "pass": 83, "fail": 0, "skip": 0, "error": 1}), CRITERIA)
    assert verdict.outcome == verdicts.INCONCLUSIVE


# ---------------------------------------------------------------- rollup


def test_a_run_that_never_reported_is_inconclusive_rather_than_graded():
    assert verdicts.verdict(facts(phase="failed"), CRITERIA).outcome == verdicts.INCONCLUSIVE


def test_a_discarded_warm_up_does_not_grade():
    assert verdicts.verdict(facts(discard=True), CRITERIA).outcome == verdicts.INCONCLUSIVE


def test_a_harness_version_outside_every_declared_range_is_not_graded_against_a_map_for_another():
    assert summaries.adapter_for("aiperf", "0.3.9") is None
    assert summaries.adapter_for("aiperf", None) is None
    assert verdicts.verdict(facts(harness_version="0.3.9"), CRITERIA).outcome == verdicts.INCONCLUSIVE


def test_the_rollup_is_unforgiving_in_the_stated_order():
    def one(outcome):
        return verdicts.Check("x", "x", outcome, None, None, "<", None)

    assert verdicts.rollup((one(verdicts.PASS), one(verdicts.FAIL), one(verdicts.INCONCLUSIVE))) == verdicts.FAIL
    assert verdicts.rollup((one(verdicts.PASS), one(verdicts.INCONCLUSIVE))) == verdicts.INCONCLUSIVE
    assert verdicts.rollup((one(verdicts.PASS), one(verdicts.PASS))) == verdicts.PASS
    assert verdicts.rollup((one(verdicts.NOT_APPLICABLE),)) == verdicts.INCONCLUSIVE
    assert verdicts.rollup(()) == verdicts.INCONCLUSIVE


def test_a_request_with_no_run_that_grades_is_not_a_pass():
    assert verdicts.rollup_verdicts([]) == verdicts.INCONCLUSIVE


def test_a_request_phase_is_derived_from_its_runs():
    assert request_phase(["succeeded", "running"]) == "running"
    assert request_phase(["succeeded", "pending"]) == "pending"
    assert request_phase(["succeeded", "failed"]) == "failed"
    assert request_phase(["succeeded", "succeeded"]) == "succeeded"
    assert request_phase([]) == "pending"


# ---------------------------------------------------------------- bands


def test_the_eight_label_strings_are_the_contract():
    assert buckets.LABELS == (
        "lt6k",
        "6k_16k",
        "16k_32k",
        "32k_64k",
        "64k_128k",
        "128k_256k",
        "gt256k",
        "unknown",
    )


@pytest.mark.parametrize(
    ("tokens", "label"),
    [
        (0, "lt6k"),
        (2000, "lt6k"),
        (6 * 1024, "lt6k"),
        (6 * 1024 + 1, "6k_16k"),
        (16 * 1024, "6k_16k"),
        (16 * 1024 + 1, "16k_32k"),
        (32 * 1024, "16k_32k"),
        (64 * 1024, "32k_64k"),
        (128 * 1024, "64k_128k"),
        (256 * 1024, "128k_256k"),
        (256 * 1024 + 1, "gt256k"),
        (None, "unknown"),
        (-1, "unknown"),
    ],
)
def test_a_prompt_length_lands_in_exactly_one_band(tokens, label):
    assert buckets.band_for(tokens) == label


def run(uid, tokens, p90=None, p50=None, avg=None, requests=100, discard=False, version="0.12.0"):
    summary = None
    if p90 is not None or p50 is not None or avg is not None:
        ttft = {key: value for key, value in (("p90", p90), ("p50", p50), ("avg", avg)) if value is not None}
        summary = {"time_to_first_token": ttft, "request_count": requests}
    return SimpleNamespace(
        uid=uid,
        harness="aiperf",
        harness_version=version,
        parameters={"input_tokens_mean": tokens} if tokens is not None else {},
        summary=summary,
        discard=discard,
    )


def test_a_sweep_is_read_back_one_band_at_a_time():
    """One invocation measures one load point; the standard sets a target per band."""
    samples = [
        buckets.sample(run("bmk_1", 2000, p90=900.0, p50=400.0, avg=450.0)),
        buckets.sample(run("bmk_2", 4000, p90=1200.0, p50=500.0, avg=550.0)),
        buckets.sample(run("bmk_3", 20000, p90=5200.0, p50=2600.0, avg=2700.0)),
        buckets.sample(run("bmk_4", 200000, p90=52000.0, p50=26000.0, avg=27000.0)),
    ]
    reports = {report.band: report for report in buckets.reaggregate(samples)}
    assert list(reports) == ["lt6k", "16k_32k", "128k_256k"]
    assert reports["lt6k"].runs == 2
    assert reports["16k_32k"].runs == 1


def test_a_band_takes_the_worst_percentile_of_its_runs_because_percentiles_do_not_average():
    samples = [
        buckets.sample(run("bmk_1", 2000, p90=900.0, p50=400.0, avg=500.0)),
        buckets.sample(run("bmk_2", 4000, p90=4900.0, p50=2100.0, avg=1500.0)),
    ]
    report = buckets.reaggregate(samples)[0]
    assert report.ttft_p90_ms == 4900.0
    assert report.ttft_p50_ms == 2100.0


def test_a_bands_average_is_weighted_by_the_requests_behind_it():
    samples = [
        buckets.sample(run("bmk_1", 2000, avg=1000.0, requests=100)),
        buckets.sample(run("bmk_2", 3000, avg=2000.0, requests=300)),
    ]
    report = buckets.reaggregate(samples)[0]
    assert report.ttft_avg_ms == pytest.approx(1750.0)
    assert report.requests == 400


def test_a_discarded_warm_up_is_excluded_from_every_band():
    samples = [
        buckets.sample(run("bmk_1", 2000, p90=900.0, discard=True)),
        buckets.sample(run("bmk_2", 2000, p90=1100.0)),
    ]
    reports = buckets.reaggregate(samples)
    assert reports[0].runs == 1
    assert reports[0].ttft_p90_ms == 1100.0


def test_a_run_that_reported_no_latency_is_named_rather_than_dropped():
    """A band that quietly measured half of what it claims is the failure this read exists for."""
    samples = [
        buckets.sample(run("bmk_1", 2000, p90=900.0)),
        buckets.sample(run("bmk_2", 2000)),
        buckets.sample(run("bmk_3", 2000, p90=800.0, version="0.3.0")),
    ]
    report = buckets.reaggregate(samples)[0]
    assert report.runs == 3
    assert report.incomplete == ("bmk_2", "bmk_3")
    assert report.ttft_p90_ms == 900.0


def test_a_run_with_no_declared_prompt_length_lands_in_unknown_rather_than_nowhere():
    report = buckets.reaggregate([buckets.sample(run("bmk_1", None, p90=900.0))])[0]
    assert report.band == "unknown"


def test_a_band_nothing_landed_in_is_absent_rather_than_reported_empty():
    reports = buckets.reaggregate([buckets.sample(run("bmk_1", 2000, p90=900.0))])
    assert [report.band for report in reports] == ["lt6k"]


# ---------------------------------------------------------------- modes


def test_a_mode_is_a_shape_of_runs_and_not_a_harness():
    assert set(MODES) == {"single", "warmup", "ramp", "sweep", "soak"}
    assert set(MODES) & set(harnesses.HARNESSES) == set()


def test_a_stepped_mode_walks_a_knob_the_harness_actually_has():
    knobs = {parameter.name for parameter in harnesses.AIPERF.parameters}
    assert set(STEPPED.values()) <= knobs
    assert STEPPED["sweep"] == "input_tokens_mean"


# ---------------------------------------------------------------- request expansion


def request(**overrides):
    base = {"level": "deployment", "target": "serve"}
    base.update(overrides)
    return BenchmarkRequestCreateRequest(**base)


def test_an_unstepped_mode_is_one_run_and_takes_no_steps():
    assert steps_for(harnesses.AIPERF, request(mode="single")) == [None]
    assert steps_for(harnesses.AIPERF, request(mode="soak")) == [None]
    with pytest.raises(Invalid):
        steps_for(harnesses.AIPERF, request(mode="single", steps=[1, 2]))


def test_a_sweep_walks_prompt_length_and_a_ramp_walks_load():
    assert steps_for(harnesses.AIPERF, request(mode="sweep", steps=[2000, 20000])) == [2000, 20000]
    assert steps_for(harnesses.AIPERF, request(mode="ramp", steps=[1, 4, 16])) == [1, 4, 16]


def test_a_stepped_mode_with_no_steps_is_refused_rather_than_collapsed_to_one_run():
    """A caller who asked for a sweep and got one point has a claim covering six bands."""
    with pytest.raises(Invalid):
        steps_for(harnesses.AIPERF, request(mode="sweep"))


def test_repeated_steps_are_collapsed_and_a_non_positive_one_is_refused():
    assert steps_for(harnesses.AIPERF, request(mode="ramp", steps=[4, 4, 8])) == [4, 8]
    with pytest.raises(Invalid):
        steps_for(harnesses.AIPERF, request(mode="ramp", steps=[0]))


def test_a_harness_with_no_such_knob_cannot_be_run_in_a_stepped_mode():
    with pytest.raises(Invalid):
        steps_for(harnesses.CONFORMANCE, request(level="endpoint", mode="sweep", steps=[2000]))


def test_an_unknown_mode_is_refused():
    with pytest.raises(Invalid):
        steps_for(harnesses.AIPERF, request(mode="stress"))


def test_a_service_tier_is_normalised_and_an_unknown_one_is_refused():
    assert tier_for("l1") == "L1"
    assert tier_for(None) is None
    assert tier_for("  ") is None
    with pytest.raises(Invalid):
        tier_for("gold")


def test_coverage_is_homogeneous_only_when_every_entry_agrees():
    one = {"artifact_id": "art_1", "recipe_id": "rec_1", "recipe_version": 3, "deployment_id": "dep_1"}
    same = dict(one, deployment_id="dep_2")
    other = dict(one, recipe_version=4)
    assert homogeneous([one, same]) is True
    assert homogeneous([one, other]) is False
    assert homogeneous([]) is True


def test_the_request_surface_is_a_collection_beside_the_runs_and_not_a_subresource():
    methods = {name for name, member in vars(BenchmarkRoutes).items() if hasattr(member, "fn")}
    assert {"create_request", "get_request", "get_request_verdict", "get_request_bands", "get_verdict"} <= methods
    assert "update_request" not in methods
    assert "delete_request" not in methods


def test_the_harness_catalogue_serves_the_levels_as_well_as_the_knobs():
    served = {harness.name: harness for harness in BenchmarkService(db=None).harnesses()}
    assert served["conformance"].levels == ["endpoint", "deployment"]
    assert served["conformance"].max_probes == 200
    failover = next(p for p in served["conformance"].parameters if p.name == "failover_probe")
    assert failover.levels == ["endpoint"]
    assert next(p for p in served["evalscope"].parameters if p.name == "temperature").type == "float"


@pytest.mark.asyncio
async def test_a_harness_at_a_level_it_does_not_grade_at_is_refused_before_the_database():
    """The same rule the parameter table already applies to knobs, applied to targets."""
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).create_request(request(level="endpoint", harness="aiperf", target="end_x"))
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).create_request(request(level="workload", harness="conformance", target="wkl_x"))


@pytest.mark.asyncio
async def test_an_unknown_criteria_version_is_refused_before_the_database():
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).create_request(request(criteria="acceptance-2027.1"))
