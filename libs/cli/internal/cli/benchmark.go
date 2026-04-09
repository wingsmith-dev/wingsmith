package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
	"github.com/hangar/cli/internal/output"
	"github.com/hangar/cli/internal/ui"
	"github.com/spf13/cobra"
)

func benchmarkNoun() noun[api.BenchmarkRun] {
	return noun[api.BenchmarkRun]{
		name:    "benchmark",
		aliases: []string{"benchmarks", "bench", "bm"},
		group:   "machinery",
		short:   "Measurements taken against a deployment",
		long: `A benchmark run is the record of one measurement and its result.

The run is carried out by an operation — that is where the phases, the deadline
and the cancellation live, and 'hgr operation cancel' against the operation named
on the run is how one is stopped. What the run holds that an operation cannot is
the result, because a load generator emits nested JSON and an operation's result
is a flat string map.

The result is stored in the harness's own shape and is not normalised. Two runs
compare when their harness and version match; across harnesses there is no shared
definition to compare against, and this CLI does not invent one. Use -o json and
jq to read a summary whose shape you know.`,
		example: `  hgr benchmark ls --deployment llama-70b-prod
  hgr benchmark run llama-70b-prod --param concurrency=64 --wait
  hgr benchmark get bmk_01m0dj…
  hgr benchmark get bmk_01m0dj… -o json | jq .summary.time_to_first_token`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.BenchmarkRun], error) {
			return b.Benchmarks(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.BenchmarkRun, error) {
			return b.Benchmark(ctx, ref)
		},
		keys:   func(r api.BenchmarkRun) []string { return []string{r.ID} },
		empty:  "no benchmark runs match",
		getArg: "<benchmark-id>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "deployment"},
			{Title: "harness"},
			{Title: "phase"},
			{Title: "load"},
			{Title: "ttft p50", Align: ui.AlignRight},
			{Title: "itl p50", Align: ui.AlignRight},
			{Title: "gen", Align: ui.AlignRight, Wide: true},
			{Title: "actor", Wide: true},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, r api.BenchmarkRun) []string {
			return []string{
				r.ID, shorten(r.DeploymentID, 18), harnessCell(r), phaseColor(s, r.Phase),
				benchmarkLoad(r), headlineStat(r, "time_to_first_token"),
				headlineStat(r, "inter_token_latency"),
				fmt.Sprintf("%d", r.Provenance.DeploymentGeneration),
				r.RequestedBy, agoAt(r.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.deployment, "deployment", "", "runs against one deployment")
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.harness, "harness", "", "filter by harness, e.g. aiperf")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by phase: pending|running|succeeded|failed|cancelled")
			cmd.Flags().StringVar(&f.actor, "actor", "", "runs requested by one actor")
			cmd.Flags().StringVar(&f.since, "since", "", "only runs created after a duration ago or an RFC 3339 time")
		},
		detail: func(app *App, w io.Writer, s ui.Style, r api.BenchmarkRun) {
			benchmarkDetail(app, w, s, r)
		},
		next: func(r api.BenchmarkRun) []errs.Action {
			if !r.Terminal() && r.OperationID != nil {
				return []errs.Action{
					{Description: "Watch the operation", Command: "hgr operation wait " + *r.OperationID},
					{Description: "Stop it", Command: "hgr operation cancel " + *r.OperationID},
				}
			}
			return []errs.Action{
				{Description: "Compare with earlier runs", Command: "hgr benchmark ls --deployment " + r.DeploymentID},
				{Description: "Read the full result", Command: "hgr benchmark get " + r.ID + " -o json | jq .summary"},
			}
		},
	}
}

func benchmarkDetail(app *App, w io.Writer, s ui.Style, r api.BenchmarkRun) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(harnessCell(r)), s.Dim(r.ID))

	fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
	kv := app.KV()
	kv.Add("deployment", r.DeploymentID)
	kv.Add("cluster", r.ClusterID)
	kv.Add("harness", r.Harness)
	kv.Add("image", r.Image)
	kv.Add("reason", orDash(str(r.Reason)))
	kv.Add("requested by", r.RequestedBy)
	kv.Add("operation", orDash(str(r.OperationID)))
	kv.Add("load", labelString(r.Parameters))
	kv.Render(w)

	// Its own block, because it is the half that makes a number mean something.
	// A run without what it measured is not comparable with anything.
	fmt.Fprintf(w, "\n%s\n", s.Dim("what was measured"))
	pv := app.KV()
	pv.Add("model", r.Provenance.Model)
	pv.Addf("deployment generation", "%d", r.Provenance.DeploymentGeneration)
	pv.Add("recipe", orDash(str(r.Provenance.RecipeID)))
	version := ""
	if r.Provenance.RecipeVersion != nil {
		version = fmt.Sprintf("%d", *r.Provenance.RecipeVersion)
	}
	pv.Add("recipe version", orDash(version))
	pv.Add("engine args", labelString(r.Provenance.EngineArgs))
	pv.Render(w)

	fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
	ov := app.KV()
	ov.Add("phase", phaseColor(s, r.Phase))
	ov.Add("harness version", orDash(str(r.HarnessVersion)))
	ov.Add("started", ago(r.StartedAt))
	ov.Add("finished", ago(r.FinishedAt))
	ov.Add("raw records", orDash(str(r.ArtifactsURI)))
	ov.Add("error", orDash(str(r.Error)))
	ov.Render(w)

	printSummary(app, w, s, r)
}

// printSummary renders whatever the harness emitted, without claiming to know it.
//
// Anything shaped like a statistic block becomes a row of percentiles, because
// that is the shape load generators converge on and it is what is worth reading
// at a glance. Everything else is named and left as JSON — which is the honest
// rendering of a field nothing promised the shape of, and the reason -o json is
// pointed at in the help rather than treated as a fallback.
func printSummary(app *App, w io.Writer, s ui.Style, r api.BenchmarkRun) {
	if len(r.Summary) == 0 || string(r.Summary) == "null" {
		return
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(r.Summary, &decoded); err != nil {
		fmt.Fprintf(w, "\n%s\n%s\n", s.Dim("result"), string(r.Summary))
		return
	}

	fmt.Fprintf(w, "\n%s\n", s.Dim("result"))
	names := make([]string, 0, len(decoded))
	for name := range decoded {
		names = append(names, name)
	}
	sort.Strings(names)

	kv := app.KV()
	for _, name := range names {
		if block, ok := statBlock(decoded[name]); ok {
			kv.Add(strings.ReplaceAll(name, "_", " "), block.line())
			continue
		}
		kv.Add(strings.ReplaceAll(name, "_", " "), strings.TrimSpace(string(decoded[name])))
	}
	kv.Render(w)
}

// stats is a statistic block as harnesses tend to emit one. It is a guess by
// construction and is treated as one: a payload that does not fit falls through
// to being printed as the JSON it is.
type stats struct {
	Unit *string  `json:"unit"`
	Avg  *float64 `json:"avg"`
	Min  *float64 `json:"min"`
	Max  *float64 `json:"max"`
	P50  *float64 `json:"p50"`
	P90  *float64 `json:"p90"`
	P99  *float64 `json:"p99"`
}

func statBlock(raw json.RawMessage) (stats, bool) {
	var out stats
	if err := json.Unmarshal(raw, &out); err != nil {
		return out, false
	}
	if out.Avg == nil && out.P50 == nil {
		return out, false
	}
	return out, true
}

func (s stats) line() string {
	unit := ""
	if s.Unit != nil {
		unit = " " + *s.Unit
	}
	parts := []string{}
	for _, pair := range []struct {
		label string
		value *float64
	}{{"avg", s.Avg}, {"p50", s.P50}, {"p90", s.P90}, {"p99", s.P99}, {"max", s.Max}} {
		if pair.value != nil {
			parts = append(parts, fmt.Sprintf("%s %s", pair.label, trimFloat(*pair.value)))
		}
	}
	return strings.Join(parts, " · ") + unit
}

func trimFloat(v float64) string {
	if v == float64(int64(v)) {
		return fmt.Sprintf("%d", int64(v))
	}
	return fmt.Sprintf("%.2f", v)
}

// headlineStat is one number for a table cell, or a dash. A dash here is not an
// error: the plane promises nothing about a summary's keys, and a harness that
// names its metrics differently is still perfectly readable with `get -o json`.
func headlineStat(r api.BenchmarkRun, name string) string {
	if len(r.Summary) == 0 {
		return "-"
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(r.Summary, &decoded); err != nil {
		return "-"
	}
	raw, ok := decoded[name]
	if !ok {
		return "-"
	}
	block, ok := statBlock(raw)
	if !ok {
		return "-"
	}
	if block.P50 != nil {
		return trimFloat(*block.P50)
	}
	if block.Avg != nil {
		return trimFloat(*block.Avg)
	}
	return "-"
}

func harnessCell(r api.BenchmarkRun) string {
	if r.HarnessVersion != nil && *r.HarnessVersion != "" {
		return r.Harness + " " + *r.HarnessVersion
	}
	return r.Harness
}

// benchmarkLoad is the three knobs that change what was measured most.
func benchmarkLoad(r api.BenchmarkRun) string {
	parts := []string{}
	for _, pair := range []struct{ prefix, key string }{
		{"c=", "concurrency"}, {"n=", "request_count"},
		{"in=", "input_tokens_mean"}, {"out=", "output_tokens_mean"},
	} {
		if value, ok := r.Parameters[pair.key]; ok {
			parts = append(parts, pair.prefix+fmt.Sprintf("%v", trimNumber(value)))
		}
	}
	if len(parts) == 0 {
		return "-"
	}
	return strings.Join(parts, " ")
}

// trimNumber keeps `64` from printing as `64` via a float that says `6.4e+01`.
func trimNumber(value any) any {
	if f, ok := value.(float64); ok && f == float64(int64(f)) {
		return int64(f)
	}
	return value
}

// newBenchmarkCmd adds the one verb: starting a run.
func newBenchmarkCmd(app *App) *cobra.Command {
	cmd := benchmarkNoun().command(app)
	cmd.AddCommand(newBenchmarkRunCmd(app), newBenchmarkHarnessCmd(app))
	return cmd
}

func newBenchmarkRunCmd(app *App) *cobra.Command {
	var (
		harness  string
		image    string
		reason   string
		deadline string
		params   []string
		wait     bool
		interval string
	)
	cmd := &cobra.Command{
		Use:     "run <deployment>",
		Aliases: []string{"start", "create"},
		Short:   "Measure a deployment with a load generator",
		Long: `Start a benchmark run against one deployment.

The load is generated inside the cluster and aimed at that cluster's own
inference endpoint. It never crosses the control plane — this command asks for a
measurement and reads the result back, and no request it causes is made from
here.

--param takes whatever the named harness accepts; 'hgr benchmark harnesses'
lists them with their defaults. A parameter the harness does not accept is
refused here rather than becoming a job that fails in a cluster ten minutes from
now.

--image overrides the harness's pinned image for this run alone. Leave it alone
unless you are reaching for a release the plane has not pinned yet: runs that
did not use the same image are not the same comparison group.`,
		Example: `  hgr benchmark run llama-70b-prod
  hgr benchmark run llama-70b-prod --param concurrency=64 --param request_count=500 --wait
  hgr benchmark run dep_01m0dj… --param output_tokens_mean=1024 --reason "long-generation check"`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			kv, err := api.ParseKV(params, "param")
			if err != nil {
				return err
			}
			parameters := map[string]any{}
			for k, v := range kv {
				parameters[k] = v
			}

			ctx, cancel := app.RequestContext()
			defer cancel()

			req := api.BenchmarkRequest{
				Deployment: args[0], Harness: harness, Image: image, Reason: reason, Parameters: parameters,
			}
			if deadline != "" {
				at, err := parseTime(deadline, "deadline")
				if err != nil {
					return err
				}
				req.Deadline = at
			}
			run, err := app.Backend.CreateBenchmark(ctx, req)
			if err != nil {
				return err
			}
			if !wait {
				return app.emitBenchmark(cmd, run)
			}
			every, err := pollInterval(interval)
			if err != nil {
				return err
			}
			return app.waitForBenchmark(cmd, run.ID, every)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&harness, "harness", "aiperf", "which load generator to run")
	fl.StringVar(&image, "image", "", "override the harness image for this run (default: the harness's pinned image)")
	fl.StringVar(&reason, "reason", "", "why this measurement is being taken — it outlives the person who ran it")
	fl.StringVar(&deadline, "deadline", "", "give up after a duration or at an RFC 3339 time")
	fl.StringArrayVar(&params, "param", nil, "harness parameter (KEY=VALUE, repeatable)")
	fl.BoolVar(&wait, "wait", false, "poll until the run reaches a terminal phase")
	fl.StringVar(&interval, "interval", "5s", "how often to poll while waiting")
	return cmd
}

func newBenchmarkHarnessCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "harnesses",
		Aliases: []string{"harness"},
		Short:   "What can be run, and what it accepts",
		Long: `List the load generators the plane knows about and their parameters.

Asked of the plane rather than compiled in, so this is the same table the plane
validates --param against and there is no second answer to what AIPerf takes.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			harnesses, err := app.Backend.BenchmarkHarnesses(ctx)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    harnesses,
				Human: func(w io.Writer, s ui.Style, width int) {
					for i, harness := range harnesses {
						if i > 0 {
							fmt.Fprintln(w)
						}
						fmt.Fprintf(w, "%s %s\n", s.Bold(harness.Name), s.Dim(harness.Image))
						fmt.Fprintf(w, "%s\n\n", harness.Description)
						kv := app.KV()
						for _, parameter := range harness.Parameters {
							kv.Addf(parameter.Name, "%v  %s", parameter.Default, s.Dim(parameter.Description))
						}
						kv.Render(w)
					}
				},
			})
		},
	}
}

// waitForBenchmark polls one run to a terminal phase, reporting phase changes
// rather than every poll.
func (a *App) waitForBenchmark(cmd *cobra.Command, ref string, every time.Duration) error {
	ctx, cancel := a.RequestContext()
	defer cancel()

	step := a.Report.Step("waiting for " + ref)
	last := ""
	for {
		run, err := a.Backend.Benchmark(ctx, ref)
		if err != nil {
			step.Fail("failed")
			return err
		}
		if run.Phase != last {
			step.Update("%s · %s", run.Harness, run.Phase)
			last = run.Phase
		}
		if run.Terminal() {
			if run.Phase == api.OpSucceeded {
				step.Done("%s %s", run.Harness, run.Phase)
				return a.emitBenchmark(cmd, run)
			}
			step.Fail("%s %s", run.Harness, run.Phase)
			e := errs.New(errs.CodeInvalidState, "benchmark %s ended as %s", run.ID, run.Phase)
			if run.Error != nil {
				e = e.WithCause(*run.Error)
			}
			return e.WithFix("Inspect it", "hgr benchmark get "+run.ID)
		}
		select {
		case <-ctx.Done():
			step.Fail("timed out")
			return errs.New(errs.CodeTimeout, "benchmark %s was still %s after %s", run.ID, run.Phase, a.Timeout).
				WithFix("Keep watching", "hgr benchmark get "+run.ID)
		case <-time.After(every):
		}
	}
}

func (a *App) emitBenchmark(cmd *cobra.Command, run api.BenchmarkRun) error {
	return a.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    run,
		Names:   []string{run.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			benchmarkDetail(a, w, s, run)
		},
		Next: benchmarkNoun().nextFor(run),
	})
}
