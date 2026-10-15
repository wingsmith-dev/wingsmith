package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
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

The result is stored in the harness's own format and is not normalised. Runs
are comparable when their harness and version match. Use -o json and jq to read
a result.`,
		example: `  hgr benchmark ls --deployment chat
  hgr benchmark run chat --param concurrency=64 --wait
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
				r.ID, shorten(r.Target(), 18), harnessCell(r), phaseColor(s, r.Phase),
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
				{Description: "Compare with earlier runs", Command: "hgr benchmark ls --deployment " + str(r.DeploymentID)},
				{Description: "Read the full result", Command: "hgr benchmark get " + r.ID + " -o json | jq .summary"},
			}
		},
	}
}

func benchmarkDetail(app *App, w io.Writer, s ui.Style, r api.BenchmarkRun) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(harnessCell(r)), s.Dim(r.ID))

	fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
	kv := app.KV()
	kv.Add("deployment", orDash(str(r.DeploymentID)))
	kv.Add("cluster", orDash(str(r.ClusterID)))
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
	pv.Add("serving digest", orDash(str(r.Provenance.ServingDigest)))
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

// newBenchmarkCmd adds the verbs a run and its request carry: starting a run,
// listing the harnesses, deriving a verdict, and the request that fans out into
// many runs at once.
func newBenchmarkCmd(app *App) *cobra.Command {
	cmd := benchmarkNoun().command(app)
	cmd.AddCommand(
		newBenchmarkRunCmd(app),
		newBenchmarkHarnessCmd(app),
		newBenchmarkVerdictCmd(app),
		newBenchmarkRequestCmd(app),
	)
	return cmd
}

// newBenchmarkVerdictCmd derives one run's verdict against a criteria version.
//
// The verdict is derived at read and stored nowhere, so a criteria version is
// always part of the answer: the same run grades differently against a later
// standard, and a verdict without the standard it was reached under is a number
// with no meaning.
func newBenchmarkVerdictCmd(app *App) *cobra.Command {
	var criteria string
	cmd := &cobra.Command{
		Use:   "verdict <benchmark-id>",
		Short: "Grade one run against the acceptance standard",
		Long: `Derive a verdict for one benchmark run.

Nothing is stored: the checks are recomputed against the criteria version named
here, defaulting to whatever the plane currently publishes. Pass --criteria to
grade against an older standard deliberately.`,
		Example: `  hgr benchmark verdict bmk_01m…
  hgr benchmark verdict bmk_01m… --criteria acceptance-2026.1`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			run, err := benchmarkNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			verdict, err := app.Backend.BenchmarkVerdict(ctx, run.ID, criteria)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    verdict,
				Human: func(w io.Writer, s ui.Style, width int) {
					benchmarkVerdict(app, w, s, verdict)
				},
			})
		},
	}
	cmd.Flags().StringVar(&criteria, "criteria", "", "grade against a criteria version (default: the current one)")
	return cmd
}

func newBenchmarkRequestCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "request",
		Aliases: []string{"requests", "req"},
		Short:   "The ask that fans out into many runs",
		Long: `A benchmark request is one ask — a level, a target, a harness and a mode —
that the plane expands into a run per input-length band.

Its verdict is the rollup of the runs behind it, and its bands are those runs
grouped by the load they measured. Like a run's, both are derived at read
against a criteria version and stored nowhere.`,
	}
	cmd.AddCommand(
		newBenchmarkRequestCreateCmd(app),
		newBenchmarkRequestGetCmd(app),
		newBenchmarkRequestVerdictCmd(app),
		newBenchmarkRequestBandsCmd(app),
	)
	return cmd
}

func newBenchmarkRequestCreateCmd(app *App) *cobra.Command {
	var (
		level    string
		harness  string
		image    string
		mode     string
		steps    []int
		criteria string
		tier     string
		reason   string
		params   []string
	)
	cmd := &cobra.Command{
		Use:     "create <target>",
		Aliases: []string{"new"},
		Short:   "Ask for a measurement at a level, expanded into runs",
		Long: `Create a benchmark request. The target is a deployment, a workload or an
endpoint depending on --level, and the plane fans the ask out into one run per
input-length band.

--criteria is left blank to pin whatever standard the plane currently publishes;
a mode of ramp or sweep walks the --steps load points in order.`,
		Example: `  hgr benchmark request create dep_01m… --level deployment
  hgr benchmark request create chat --level endpoint --mode sweep --steps 1000,8000,24000`,
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
			req := api.BenchmarkRequestCreate{
				Level: level, Target: args[0], Harness: harness, Image: image,
				Mode: mode, Steps: steps, Criteria: criteria, Tier: tier,
				Reason: reason, Parameters: parameters,
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			request, err := app.Backend.CreateBenchmarkRequest(ctx, req)
			if err != nil {
				return err
			}
			return app.emitBenchmarkRequest(cmd, request)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&level, "level", "deployment", "what the target is: deployment|workload|endpoint")
	fl.StringVar(&harness, "harness", "aiperf", "which load generator to run")
	fl.StringVar(&image, "image", "", "override the harness image for these runs")
	fl.StringVar(&mode, "mode", "single", "single|warmup|ramp|sweep")
	fl.IntSliceVar(&steps, "steps", nil, "load points a ramp or sweep walks, in order")
	fl.StringVar(&criteria, "criteria", "", "pin a criteria version (default: the current one)")
	fl.StringVar(&tier, "tier", "", "service tier the standard grades against, e.g. L1")
	fl.StringVar(&reason, "reason", "", "why this measurement is being taken")
	fl.StringArrayVar(&params, "param", nil, "harness parameter (KEY=VALUE, repeatable)")
	return cmd
}

func newBenchmarkRequestGetCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "get <request-id>",
		Aliases: []string{"show", "describe"},
		Short:   "Show one benchmark request and the runs it became",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			request, err := app.Backend.BenchmarkRequest(ctx, args[0])
			if err != nil {
				return err
			}
			return app.emitBenchmarkRequest(cmd, request)
		},
	}
}

func newBenchmarkRequestVerdictCmd(app *App) *cobra.Command {
	var criteria string
	cmd := &cobra.Command{
		Use:   "verdict <request-id>",
		Short: "The rollup verdict across a request's runs",
		Long: `Derive the verdict for a whole request: the rollup of every run behind it,
graded against a criteria version and stored nowhere.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			verdict, err := app.Backend.BenchmarkRequestVerdict(ctx, args[0], criteria)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    verdict,
				Human: func(w io.Writer, s ui.Style, width int) {
					benchmarkVerdict(app, w, s, verdict)
				},
			})
		},
	}
	cmd.Flags().StringVar(&criteria, "criteria", "", "grade against a criteria version (default: the current one)")
	return cmd
}

func newBenchmarkRequestBandsCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "bands <request-id>",
		Aliases: []string{"band"},
		Short:   "A request's runs grouped by the load they measured",
		Long: `Show a request's runs as the input-length bands they cover, with the
time-to-first-token each band reached and the ones a band is still missing. A
band with incomplete runs is a coverage gap, not a pass.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			bands, err := app.Backend.BenchmarkRequestBands(ctx, args[0])
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    bands,
				Human: func(w io.Writer, s ui.Style, width int) {
					benchmarkBands(app, w, s, bands)
				},
			})
		},
	}
}

func (a *App) emitBenchmarkRequest(cmd *cobra.Command, request api.BenchmarkRequest) error {
	return a.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    request,
		Names:   []string{request.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			benchmarkRequestDetail(a, w, s, request)
		},
		Next: []errs.Action{
			{Description: "Its verdict", Command: "hgr benchmark request verdict " + request.ID},
			{Description: "Its bands", Command: "hgr benchmark request bands " + request.ID},
		},
	})
}

func benchmarkRequestDetail(app *App, w io.Writer, s ui.Style, r api.BenchmarkRequest) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(r.Level+" · "+r.Harness), s.Dim(r.ID))

	kv := app.KV()
	kv.Add("level", r.Level)
	kv.Add("target", r.TargetID)
	kv.Add("harness", r.Harness)
	kv.Add("image", orDash(r.Image))
	kv.Add("mode", r.Mode)
	kv.Add("criteria version", r.CriteriaVersion)
	kv.Add("tier", orDash(str(r.Tier)))
	kv.Add("homogeneous", boolWord(r.Homogeneous))
	kv.Add("phase", phaseColor(s, r.Phase))
	kv.Addf("runs", "%d", len(r.Runs))
	kv.Add("requested by", r.RequestedBy)
	kv.Add("reason", orDash(str(r.Reason)))
	kv.Add("created", agoAt(r.CreatedAt))
	kv.Render(w)
}

// benchmarkVerdict draws a derived verdict: the outcome, the standard it was
// reached under, and every graded check with the number it graded beside the
// one it graded against.
func benchmarkVerdict(app *App, w io.Writer, s ui.Style, v api.BenchmarkVerdict) {
	fmt.Fprintf(w, "%s %s\n", verdictWord(s, v.Outcome), s.Dim("· "+v.CriteriaVersion))
	if v.Reason != nil && *v.Reason != "" {
		fmt.Fprintf(w, "%s\n", s.Dim(*v.Reason))
	}
	if len(v.Checks) == 0 {
		return
	}
	fmt.Fprintf(w, "\n%s\n", s.Dim("checks"))
	t := app.Table(
		ui.Column{Title: "check"},
		ui.Column{Title: "outcome"},
		ui.Column{Title: "observed", Align: ui.AlignRight},
		ui.Column{Title: "comparator"},
		ui.Column{Title: "threshold", Align: ui.AlignRight},
		ui.Column{Title: "detail"},
	)
	for _, c := range v.Checks {
		t.Row(c.Label, verdictWord(s, c.Outcome), floatCell(c.Observed),
			c.Comparator, floatCell(c.Threshold), orDash(str(c.Detail)))
	}
	t.Render(w)
}

func benchmarkBands(app *App, w io.Writer, s ui.Style, bands []api.BenchmarkBand) {
	if len(bands) == 0 {
		fmt.Fprintf(w, "%s\n", s.Dim("no runs to band"))
		return
	}
	t := app.Table(
		ui.Column{Title: "band"},
		ui.Column{Title: "runs", Align: ui.AlignRight},
		ui.Column{Title: "requests", Align: ui.AlignRight},
		ui.Column{Title: "ttft p50", Align: ui.AlignRight},
		ui.Column{Title: "ttft p90", Align: ui.AlignRight},
		ui.Column{Title: "coverage"},
	)
	for _, b := range bands {
		coverage := s.Green("complete")
		if len(b.Incomplete) > 0 {
			coverage = s.Amber(fmt.Sprintf("%d incomplete", len(b.Incomplete)))
		}
		t.Row(b.Band, strconv.Itoa(b.Runs), strconv.Itoa(b.Requests),
			msCell(b.TTFTP50MS), msCell(b.TTFTP90MS), coverage)
	}
	t.Render(w)
}

// verdictWord colours an outcome the way the console does: a pass is green, a
// fail is red, and inconclusive is warned rather than greyed — a missing
// measurement is the outcome someone has to go and fix, not a nothing.
func verdictWord(s ui.Style, outcome string) string {
	switch outcome {
	case "pass":
		return s.Green(outcome)
	case "fail":
		return s.Red(outcome)
	case "inconclusive":
		return s.Amber(outcome)
	case "not_applicable":
		return s.Dim("not applicable")
	default:
		return outcome
	}
}

func floatCell(p *float64) string {
	if p == nil {
		return "-"
	}
	return trimFloat(*p)
}

func msCell(p *float64) string {
	if p == nil {
		return "-"
	}
	return trimFloat(*p) + "ms"
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

The load is generated inside the cluster against that cluster's own inference
endpoint. No load passes through the control plane or this machine.

--param takes whatever the named harness accepts; 'hgr benchmark harnesses'
lists them with their defaults. A parameter the harness does not accept is
refused before the run starts.

--image overrides the harness's pinned image for this run alone. Leave it alone
unless you are reaching for a release the plane has not pinned yet: runs that
did not use the same image are not the same comparison group.`,
		Example: `  hgr benchmark run chat
  hgr benchmark run chat --param concurrency=64 --param request_count=500 --wait
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

			req := api.BenchmarkRunCreate{
				Deployment: args[0], Harness: harness, Image: image, Reason: reason, Parameters: parameters,
			}
			if deadline != "" {
				at, err := parseDeadline(deadline, "deadline")
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

This is the table the plane validates --param against.`,
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
	step := a.Report.Step("waiting for " + ref)
	last := ""
	var run api.BenchmarkRun
	expired, err := a.poll(every, func(ctx context.Context) (bool, error) {
		var err error
		run, err = a.Backend.Benchmark(ctx, ref)
		if err != nil {
			return false, err
		}
		if run.Phase != last {
			step.Update("%s · %s", run.Harness, run.Phase)
			last = run.Phase
		}
		return run.Terminal(), nil
	})
	if err != nil {
		e := errs.As(err)
		step.Fail("%s", strings.ToLower(string(e.Code)))
		if expired && run.ID != "" {
			e = errs.New(errs.CodeTimeout, "benchmark %s was still %s after %s", run.ID, run.Phase, a.WaitTimeout).
				WithCause(e.Cause).
				WithFix("Keep watching", "hgr benchmark get "+run.ID)
		}
		return e
	}
	if run.Phase == api.OpSucceeded {
		step.Done("%s %s", run.Harness, run.Phase)
		return a.emitBenchmark(cmd, run)
	}
	step.Fail("%s %s", run.Harness, run.Phase)
	e := errs.New(errs.CodeOperationFailed, "benchmark %s ended as %s", run.ID, run.Phase)
	if run.Error != nil {
		e = e.WithCause(*run.Error)
	}
	return e.WithFix("Inspect it", "hgr benchmark get "+run.ID)
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
