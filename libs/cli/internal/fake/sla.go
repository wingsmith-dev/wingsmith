package fake

import (
	"context"
	"hash/fnv"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
)

// The SLA report's period and verdict, transcribed from the plane
// (`app/modules/metrics/sla.py`) so that `--fake` refuses the periods the
// server refuses and judges a commitment the way the server does. The figures
// themselves are synthetic, and stable per endpoint so a re-run reads the same.

const maxSLAPeriod = 93 * 24 * time.Hour

const availabilityDefinition = "Live traffic only. Failed: shed, no_upstream, upstream_error, upstream_timeout, " +
	"internal_error. Counted: every outcome except canceled, so a caller's own 4xx and a 429 over its declared " +
	"limit count as served, and a caller hanging up counts as neither."

func (b *Backend) EndpointSLA(ctx context.Context, ref string, q api.SLAQuery) (api.SLAReport, error) {
	if err := b.injected(); err != nil {
		return api.SLAReport{}, err
	}
	endpoint, err := b.Endpoint(ctx, ref)
	if err != nil {
		return api.SLAReport{}, err
	}
	start, end, partial, err := slaPeriod(q, time.Now().UTC())
	if err != nil {
		return api.SLAReport{}, err
	}

	h := fnv.New32a()
	_, _ = h.Write([]byte(endpoint.ID))
	seed := float64(h.Sum32()%1000) / 1000

	failure := 0.01 + seed*0.12
	availability := 100 - failure
	requests := int64((2 + seed*6) * end.Sub(start).Seconds())
	report := api.SLAReport{
		Endpoint: endpoint.ID, EndpointName: endpoint.Name,
		PeriodStart: start, PeriodEnd: end, Partial: partial,
		Definition: defaultAvailabilityObjective, DefinitionText: availabilityDefinition,
		Requests: &requests, Availability: &availability, FailureRatio: &failure,
		Latency: percentiles(0.9+seed, 4.2+seed*3, 9.5+seed*6),
		TTFT:    percentiles(0.18+seed/5, 0.6+seed/2, 1.4+seed),
	}
	for _, quota := range b.state.ServingQuotas {
		if quota.Endpoint != endpoint.ID {
			continue
		}
		report.Objective = &api.SLAObjective{
			SuccessRate: quota.SuccessRateObjective, WindowSeconds: quota.ObjectiveWindowSeconds, Enforced: quota.Enforced,
		}
		report.Met, report.ErrorBudgetConsumed = slaVerdict(failure, quota.SuccessRateObjective)
	}
	return report, nil
}

func percentiles(p50, p95, p99 float64) api.SLAPercentiles {
	return api.SLAPercentiles{P50: &p50, P95: &p95, P99: &p99}
}

// slaPeriod resolves what the caller asked for the way the plane does: a
// month or a range, never both, clamped to now.
func slaPeriod(q api.SLAQuery, now time.Time) (start, end time.Time, partial bool, err error) {
	switch {
	case q.Month != "" && (q.From != nil || q.To != nil):
		return start, end, false, errs.New(errs.CodeUsage, "give either --month or --from/--to, not both")
	case q.Month != "":
		month, perr := time.Parse("2006-01", q.Month)
		if perr != nil {
			return start, end, false, errs.New(errs.CodeUsage, "--month is YYYY-MM")
		}
		start, end = month, month.AddDate(0, 1, 0)
	case q.From == nil && q.To == nil:
		start = time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
		end = start.AddDate(0, 1, 0)
	case q.From == nil:
		return start, end, false, errs.New(errs.CodeUsage, "--to needs a --from")
	default:
		start, end = q.From.UTC(), now
		if q.To != nil {
			end = q.To.UTC()
		}
	}
	switch {
	case !start.Before(now):
		return start, end, false, errs.New(errs.CodeUsage, "the period has not started yet")
	case !end.After(start):
		return start, end, false, errs.New(errs.CodeUsage, "the period ends before it starts")
	case end.Sub(start) > maxSLAPeriod:
		return start, end, false, errs.New(errs.CodeUsage, "a report covers at most 93 days")
	}
	if end.After(now) {
		return start, now, true, nil
	}
	return start, end, false, nil
}

// slaVerdict is the plane's: the objective is a fraction, the ratio a
// percentage, and the budget is the failure the objective allows.
func slaVerdict(failure float64, objective *float64) (*bool, *float64) {
	if objective == nil {
		return nil, nil
	}
	allowed := (1 - *objective) * 100
	met := failure <= allowed
	if allowed <= 0 {
		return &met, nil
	}
	consumed := failure / allowed
	return &met, &consumed
}
