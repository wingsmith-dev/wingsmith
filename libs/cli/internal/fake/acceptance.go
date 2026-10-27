package fake

import (
	"encoding/json"
	"fmt"
	"sort"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
)

// The acceptance standard, transcribed from the plane's frozen criteria table
// so that `--fake` grades a run the way the server would. A copy, and knowingly
// one: the fake exists to answer offline and there is no plane to ask. Drift
// shows up as a verdict the real plane disagrees with, which is a bug in the
// fake and not in the design.

const criteriaVersion = "acceptance-2026.1"

// ttftTarget is the three time-to-first-token ceilings one input band is held
// to, in milliseconds.
type ttftTarget struct {
	band  string
	p90MS float64
	p50MS float64
	avgMS float64
}

var ttftTargets = []ttftTarget{
	{"lt6k", 5_000, 2_000, 2_000},
	{"6k_16k", 5_000, 2_500, 4_000},
	{"16k_32k", 8_000, 4_000, 6_000},
	{"32k_64k", 15_000, 8_000, 8_000},
	{"64k_128k", 35_000, 15_000, 15_000},
	{"128k_256k", 70_000, 30_000, 30_000},
}

// outputSpeedLarge is the per-stream floor a model above largeParameterCount is
// held to, by service tier. Per stream and never aggregate: 30 tokens a second
// aggregate on a serving GPU would itself be a fault.
var outputSpeedLarge = map[string]float64{"L1": 30, "L2": 10}

const (
	outputSpeedSmall    = 100.0
	largeParameterCount = int64(10_000_000_000)
)

// verdictFor grades one run, or says why it cannot be graded. An `inconclusive`
// is first-class here and is what a missing measurement produces: it is never a
// pass.
func verdictFor(run api.BenchmarkRun, parameterCount *int64, tier *string, criteria string) api.BenchmarkVerdict {
	if criteria == "" {
		criteria = criteriaVersion
	}
	if run.Discard {
		return inconclusive(criteria, "this run is marked discard and does not grade")
	}
	if run.Phase != api.OpSucceeded {
		return inconclusive(criteria, fmt.Sprintf("the run is %s and reported no result", run.Phase))
	}

	summary := decodeSummary(run.Summary)
	if summary == nil {
		return inconclusive(criteria, "the run carries no summary")
	}

	band := run.Band()
	checks := ttftChecks(summary, band)
	checks = append(checks, outputSpeedCheck(summary, parameterCount, tier))
	return api.BenchmarkVerdict{Outcome: rollup(checks), CriteriaVersion: criteria, Checks: checks}
}

func ttftChecks(summary map[string]any, band string) []api.BenchmarkCheck {
	var target *ttftTarget
	for i := range ttftTargets {
		if ttftTargets[i].band == band {
			target = &ttftTargets[i]
			break
		}
	}
	if target == nil {
		outcome, detail := api.VerdictNotApplicable, criteriaVersion+" sets no time-to-first-token target above 256K"
		if band == "" {
			band, outcome = api.BandUnknown, api.VerdictInconclusive
			detail = "the run declared no input length, so no band applies"
		}
		return []api.BenchmarkCheck{{
			ID: "ttft", Label: "time to first token (" + band + ")",
			Outcome: outcome, Comparator: "<", Detail: strptr(detail),
		}}
	}
	return []api.BenchmarkCheck{
		below(summary, "ttft.p90", "time_to_first_token", "p90", band, target.p90MS),
		below(summary, "ttft.p50", "time_to_first_token", "p50", band, target.p50MS),
		below(summary, "ttft.avg", "time_to_first_token", "avg", band, target.avgMS),
	}
}

func below(summary map[string]any, id, block, stat, band string, ceiling float64) api.BenchmarkCheck {
	label := fmt.Sprintf("%s (%s)", id, band)
	value, ok := stat64(summary, block, stat)
	if !ok {
		return api.BenchmarkCheck{
			ID: id, Label: label, Outcome: api.VerdictInconclusive, Threshold: &ceiling,
			Comparator: "<", Detail: strptr(block + "." + stat + " is not in the summary"),
		}
	}
	outcome := api.VerdictFail
	if value < ceiling {
		outcome = api.VerdictPass
	}
	return api.BenchmarkCheck{
		ID: id, Label: label, Outcome: outcome,
		Observed: &value, Threshold: &ceiling, Comparator: "<",
	}
}

// outputSpeedCheck reads the per-stream generation rate, never the aggregate.
// The two are different measurements: the aggregate rises with scale and the
// per-stream one is what a single reader observes, which is what a floor of
// "30 tokens a second" means.
func outputSpeedCheck(summary map[string]any, parameterCount *int64, tier *string) api.BenchmarkCheck {
	const id = "output_speed.per_stream"
	const label = "output speed per stream"

	if parameterCount == nil {
		return api.BenchmarkCheck{ID: id, Label: label, Outcome: api.VerdictInconclusive, Comparator: ">=",
			Detail: strptr("the artifact declares no parameter count")}
	}
	var floor float64
	if *parameterCount > largeParameterCount {
		if tier == nil {
			return api.BenchmarkCheck{ID: id, Label: label, Outcome: api.VerdictInconclusive, Comparator: ">=",
				Detail: strptr("a model above 10B parameters is graded per service tier and the request declared none")}
		}
		value, ok := outputSpeedLarge[*tier]
		if !ok {
			return api.BenchmarkCheck{ID: id, Label: label, Outcome: api.VerdictInconclusive, Comparator: ">=",
				Detail: strptr("unknown service tier " + *tier)}
		}
		floor = value
	} else {
		floor = outputSpeedSmall
	}

	value, ok := stat64(summary, "output_token_throughput_per_user", "avg")
	if !ok {
		return api.BenchmarkCheck{ID: id, Label: label, Outcome: api.VerdictInconclusive, Threshold: &floor,
			Comparator: ">=", Detail: strptr("output_token_throughput_per_user.avg is not in the summary")}
	}
	outcome := api.VerdictFail
	if value >= floor {
		outcome = api.VerdictPass
	}
	return api.BenchmarkCheck{ID: id, Label: label, Outcome: outcome,
		Observed: &value, Threshold: &floor, Comparator: ">="}
}

// rollup is unforgiving: any fail is a fail, otherwise any
// inconclusive, otherwise a pass. A run whose every check was inapplicable
// measured nothing, and nothing is not a pass.
func rollup(checks []api.BenchmarkCheck) string {
	graded := 0
	fail, unknown := false, false
	for _, c := range checks {
		if c.Outcome == api.VerdictNotApplicable {
			continue
		}
		graded++
		switch c.Outcome {
		case api.VerdictFail:
			fail = true
		case api.VerdictInconclusive:
			unknown = true
		}
	}
	switch {
	case graded == 0, unknown && !fail:
		return api.VerdictInconclusive
	case fail:
		return api.VerdictFail
	}
	return api.VerdictPass
}

// rollupVerdicts is a request's outcome, which is its runs' outcomes under the
// same rule.
func rollupVerdicts(entries []api.BenchmarkVerdict) string {
	if len(entries) == 0 {
		return api.VerdictInconclusive
	}
	unknown := false
	for _, e := range entries {
		switch e.Outcome {
		case api.VerdictFail:
			return api.VerdictFail
		case api.VerdictInconclusive:
			unknown = true
		}
	}
	if unknown {
		return api.VerdictInconclusive
	}
	return api.VerdictPass
}

// bandsOf re-aggregates a set of runs by the input-length band each landed in.
// P90 and P50 are the worst of the band because percentiles do not average;
// the average is weighted by request count, which does.
func bandsOf(runs []api.BenchmarkRun) []api.BenchmarkBand {
	index := map[string]*api.BenchmarkBand{}
	weighted := map[string]float64{}
	for _, run := range runs {
		if run.Discard {
			continue
		}
		label := run.Band()
		if label == "" {
			label = api.BandUnknown
		}
		band, ok := index[label]
		if !ok {
			band = &api.BenchmarkBand{Band: label, Incomplete: []string{}}
			index[label] = band
		}
		band.Runs++
		requests := requestCount(run)
		band.Requests += requests

		summary := decodeSummary(run.Summary)
		p90, has90 := stat64(summary, "time_to_first_token", "p90")
		p50, has50 := stat64(summary, "time_to_first_token", "p50")
		avg, hasAvg := stat64(summary, "time_to_first_token", "avg")
		if !has90 && !has50 && !hasAvg {
			band.Incomplete = append(band.Incomplete, run.ID)
			continue
		}
		if has90 && (band.TTFTP90MS == nil || p90 > *band.TTFTP90MS) {
			band.TTFTP90MS = floatptr(p90)
		}
		if has50 && (band.TTFTP50MS == nil || p50 > *band.TTFTP50MS) {
			band.TTFTP50MS = floatptr(p50)
		}
		if hasAvg && requests > 0 {
			weighted[label] += avg * float64(requests)
		}
	}

	labels := make([]string, 0, len(index))
	for label := range index {
		labels = append(labels, label)
	}
	sort.Strings(labels)
	out := make([]api.BenchmarkBand, 0, len(labels))
	for _, label := range labels {
		band := index[label]
		if total := weighted[label]; total > 0 && band.Requests > 0 {
			band.TTFTAvgMS = floatptr(total / float64(band.Requests))
		}
		out = append(out, *band)
	}
	return out
}

func requestCount(run api.BenchmarkRun) int {
	if v, ok := run.Parameters["request_count"].(float64); ok {
		return int(v)
	}
	if v, ok := run.Parameters["request_count"].(int); ok {
		return v
	}
	return 0
}

func decodeSummary(raw json.RawMessage) map[string]any {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

func stat64(summary map[string]any, block, stat string) (float64, bool) {
	nested, ok := summary[block].(map[string]any)
	if !ok {
		return 0, false
	}
	value, ok := nested[stat].(float64)
	return value, ok
}

func inconclusive(criteria, reason string) api.BenchmarkVerdict {
	return api.BenchmarkVerdict{
		Outcome: api.VerdictInconclusive, CriteriaVersion: criteria,
		Reason: &reason, Checks: []api.BenchmarkCheck{},
	}
}
