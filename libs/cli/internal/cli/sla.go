package cli

import (
	"fmt"
	"io"
	"strconv"
	"time"

	"github.com/spf13/cobra"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/output"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/ui"
)

func newEndpointSLACmd(app *App) *cobra.Command {
	var month, from, to string
	cmd := &cobra.Command{
		Use:   "sla <endpoint>",
		Short: "Service level over a month or a period, against its objective",
		Long: `Report one endpoint's service level over a period, as the front door measured it.

Availability, failure ratio, request count and the latency and time-to-first-token
tails, all over live traffic and under the availability definition the endpoint's
serving quota names. When the quota declares a success-rate objective, the report
says whether it was met and how much of the error budget was spent.

The period is a calendar month in UTC (--month), a range (--from/--to, RFC 3339 or
YYYY-MM-DD), or by default the current month measured up to now.`,
		Example: `  wings endpoint sla chat
  wings endpoint sla chat --month 2026-09
  wings endpoint sla chat --from 2026-09-15 --to 2026-09-22 -o json`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			query, err := slaQuery(month, from, to)
			if err != nil {
				return err
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			endpoint, err := endpointNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			report, err := app.Backend.EndpointSLA(ctx, endpoint.ID, query)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    report,
				Names:   []string{report.Endpoint},
				Human: func(w io.Writer, s ui.Style, width int) {
					slaDetail(app, w, s, report)
				},
			})
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&month, "month", "", "a calendar month in UTC, YYYY-MM")
	fl.StringVar(&from, "from", "", "start of the period, RFC 3339 or YYYY-MM-DD")
	fl.StringVar(&to, "to", "", "end of the period, RFC 3339 or YYYY-MM-DD (default now)")
	return cmd
}

func slaQuery(month, from, to string) (api.SLAQuery, error) {
	q := api.SLAQuery{Month: month}
	for _, f := range []struct {
		flag, value string
		into        **time.Time
	}{{"--from", from, &q.From}, {"--to", to, &q.To}} {
		if f.value == "" {
			continue
		}
		t, err := parseInstant(f.value)
		if err != nil {
			return q, errs.New(errs.CodeUsage, "%s %q is not RFC 3339 or YYYY-MM-DD", f.flag, f.value)
		}
		*f.into = &t
	}
	return q, nil
}

func parseInstant(value string) (time.Time, error) {
	if t, err := time.Parse(time.RFC3339, value); err == nil {
		return t, nil
	}
	return time.Parse("2006-01-02", value)
}

// slaDetail draws the report. A figure nothing measured is a dash, and a
// verdict is a word beside its colour so it reads without one.
func slaDetail(app *App, w io.Writer, s ui.Style, r api.SLAReport) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(r.EndpointName), s.Dim(r.Endpoint))
	period := fmt.Sprintf("%s %s %s", r.PeriodStart.UTC().Format(time.RFC3339), s.Arrow(), r.PeriodEnd.UTC().Format(time.RFC3339))
	if r.Partial {
		period += " " + s.Amber("(in progress)")
	}
	fmt.Fprintf(w, "%s\n", period)
	if r.Unavailable != nil {
		fmt.Fprintf(w, "%s %s\n", s.Bang(), *r.Unavailable)
	}

	kv := app.KV()
	kv.Add("requests", slaCount(r.Requests))
	kv.Add("availability", slaPercent(r.Availability, 3))
	kv.Add("failure ratio", slaPercent(r.FailureRatio, 3))
	kv.Add("latency", slaTails(s, r.Latency))
	kv.Add("ttft", slaTails(s, r.TTFT))
	if r.Objective == nil {
		kv.Add("objective", s.Dim("none declared"))
	} else {
		if r.Objective.SuccessRate != nil {
			rate := *r.Objective.SuccessRate * 100
			kv.Add("objective", slaPercent(&rate, 3))
		} else {
			kv.Add("objective", s.Dim("no success rate declared"))
		}
		kv.Add("verdict", slaVerdictWord(s, r.Met))
		kv.Add("error budget", slaBudget(s, r.ErrorBudgetConsumed))
		kv.Add("enforce", enforcementWord(s, r.Objective.Enforced))
	}
	kv.Add("definition", r.Definition)
	kv.Render(w)
}

func slaCount(p *int64) string {
	if p == nil {
		return "-"
	}
	return strconv.FormatInt(*p, 10)
}

func slaPercent(p *float64, digits int) string {
	if p == nil {
		return "-"
	}
	return strconv.FormatFloat(*p, 'f', digits, 64) + "%"
}

func slaSeconds(p *float64) string {
	if p == nil {
		return "-"
	}
	return (time.Duration(*p * float64(time.Second))).Round(time.Millisecond).String()
}

func slaTails(s ui.Style, p api.SLAPercentiles) string {
	return fmt.Sprintf("%s %s  %s %s  %s %s",
		s.Dim("p50"), slaSeconds(p.P50), s.Dim("p95"), slaSeconds(p.P95), s.Dim("p99"), slaSeconds(p.P99))
}

func slaVerdictWord(s ui.Style, met *bool) string {
	switch {
	case met == nil:
		return s.Dim("not measured")
	case *met:
		return s.Green("met")
	default:
		return s.Red("missed")
	}
}

func slaBudget(s ui.Style, consumed *float64) string {
	if consumed == nil {
		return "-"
	}
	used := strconv.FormatFloat(*consumed*100, 'f', 1, 64) + "% used"
	if *consumed > 1 {
		return s.Red(used)
	}
	if *consumed > 0.75 {
		return s.Amber(used)
	}
	return used
}
