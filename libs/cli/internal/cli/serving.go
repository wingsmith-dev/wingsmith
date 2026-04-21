package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

func servingQuotaNoun() noun[api.ServingQuota] {
	return noun[api.ServingQuota]{
		name:    "serving-quota",
		aliases: []string{"serving-quotas", "serving"},
		group:   "binding",
		short:   "Committed rate and concurrency for one endpoint",
		long: `A serving quota is one row of committed limits per endpoint: the requests
and tokens a minute a caller may count on, the concurrency and output ceilings,
and the objectives the commitment is held to.

Every limit is nullable and a null is UNCOMMITTED, never zero — an endpoint with
no quota at all is not rate limited, and an axis left unset is a promise nobody
made rather than a promise of nothing. 'enforced' is the difference between a
limit the gateway refuses on and one it only counts: shadow mode measures a
commitment before it bites.`,
		example: `  hgr serving-quota ls
  hgr serving-quota ls --enforced=false
  hgr serving-quota get chat
  hgr serving-quota create chat --committed-rpm 1000 --committed-tpm 10000000 --enforced`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.ServingQuota], error) {
			return b.ServingQuotas(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.ServingQuota, error) {
			return b.ServingQuota(ctx, ref)
		},
		keys:   func(q api.ServingQuota) []string { return []string{q.EndpointName, q.ID} },
		empty:  "no serving quotas committed",
		getArg: "<endpoint|quota-id>",
		columns: []ui.Column{
			{Title: "endpoint"},
			{Title: "id", Wide: true},
			{Title: "project", Wide: true},
			{Title: "rpm", Align: ui.AlignRight},
			{Title: "tpm", Align: ui.AlignRight},
			{Title: "concurrent", Align: ui.AlignRight},
			{Title: "max out", Align: ui.AlignRight},
			{Title: "scope"},
			{Title: "enforcement"},
			{Title: "standard"},
		},
		row: func(s ui.Style, q api.ServingQuota) []string {
			return []string{
				q.EndpointName, q.ID, q.Project,
				committedInt(s, q.CommittedRPM), committedInt64(s, q.CommittedTPM),
				committedInt(s, q.MaxConcurrentRequests), committedInt(s, q.MaxOutputTokens),
				q.LimitScope, enforcementWord(s, q.Enforced), standardWord(s, q.Standard),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.endpoint, "endpoint", "", "limit to one endpoint")
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.scope, "scope", "", "filter by limit scope: per_replica|divided")
			cmd.Flags().BoolVar(&f.enforced, "enforced", false, "only enforced quotas (--enforced=false for shadow mode)")
		},
		detail: servingQuotaDetail,
		next: func(q api.ServingQuota) []errs.Action {
			return []errs.Action{
				{Description: "How it divides across replicas", Command: "hgr serving-quota resolved " + q.EndpointName},
				{Description: "Edit it", Command: "hgr serving-quota set " + q.ID + " --committed-rpm N"},
			}
		},
		extra: func(app *App) []*cobra.Command {
			return []*cobra.Command{
				newServingQuotaCreateCmd(app),
				newServingQuotaSetCmd(app),
				newServingQuotaDeleteCmd(app),
				newServingQuotaResolvedCmd(app),
			}
		},
	}
}

func servingQuotaDetail(app *App, w io.Writer, s ui.Style, q api.ServingQuota) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(q.EndpointName), s.Dim(q.ID))

	fmt.Fprintf(w, "\n%s\n", s.Dim("committed"))
	kv := app.KV()
	kv.Add("endpoint", q.EndpointName)
	kv.Add("project", q.Project)
	kv.Add("requests / minute", committedInt(s, q.CommittedRPM))
	kv.Add("tokens / minute", committedInt64(s, q.CommittedTPM))
	kv.Add("burst rpm", committedInt(s, q.BurstRPM))
	kv.Add("max concurrent", committedInt(s, q.MaxConcurrentRequests))
	kv.Add("max output tokens", committedInt(s, q.MaxOutputTokens))
	kv.Addf("ramp up", "%ds", q.RampUpSeconds)
	kv.Add("on exceed", q.OnExceed)
	kv.Add("queue timeout", queueMS(q.QueueTimeoutMS))
	kv.Add("scope", q.LimitScope)
	kv.Add("enforcement", enforcementWord(s, q.Enforced))
	kv.Add("revision", q.Revision)
	kv.Render(w)

	fmt.Fprintf(w, "\n%s\n", s.Dim("objectives"))
	ov := app.KV()
	ov.Add("success rate", committedRate(s, q.SuccessRateObjective))
	ov.Add("availability", q.AvailabilityObjective)
	ov.Addf("window", "%ds", q.ObjectiveWindowSeconds)
	ov.Render(w)

	fmt.Fprintf(w, "\n%s\n", s.Dim("standard"))
	if q.Standard.Meets {
		fmt.Fprintf(w, "%s\n", s.Green("meets the acceptance standard"))
		return
	}
	fmt.Fprintf(w, "%s\n", s.Amber("does not meet the standard"))
	for _, shortfall := range q.Standard.Shortfalls {
		fmt.Fprintf(w, "  %s %s\n", s.Amber("·"), shortfall)
	}
}

// servingLimits binds the flags a create and a set share, and turns whichever
// were actually given into the request. Only a flag the caller changed is sent,
// so leaving one alone keeps its current value rather than committing a zero.
type servingLimits struct {
	rpm             int
	tpm             int64
	concurrent      int
	maxOutput       int
	burst           int
	rampUp          int
	queueTimeout    int
	objectiveWindow int
	onExceed        string
	scope           string
	availability    string
	successRate     float64
	enforced        bool
}

func (l *servingLimits) bind(cmd *cobra.Command) {
	fl := cmd.Flags()
	fl.IntVar(&l.rpm, "committed-rpm", 0, "requests a minute committed (unset leaves it uncommitted)")
	fl.Int64Var(&l.tpm, "committed-tpm", 0, "tokens a minute committed")
	fl.IntVar(&l.concurrent, "max-concurrent", 0, "ceiling on concurrent requests")
	fl.IntVar(&l.maxOutput, "max-output-tokens", 0, "ceiling on output tokens per request")
	fl.IntVar(&l.burst, "burst-rpm", 0, "burst ceiling above the committed rpm")
	fl.IntVar(&l.rampUp, "ramp-up-seconds", 0, "seconds to ramp to the committed rate")
	fl.StringVar(&l.onExceed, "on-exceed", "", "what a request over the limit meets: reject|queue")
	fl.IntVar(&l.queueTimeout, "queue-timeout-ms", 0, "how long a queued request waits, for --on-exceed queue")
	fl.StringVar(&l.scope, "scope", "", "whether a limit is per replica or divided across them: per_replica|divided")
	fl.Float64Var(&l.successRate, "success-rate-objective", 0, "success-rate objective, 0 to 1")
	fl.StringVar(&l.availability, "availability-objective", "", "availability objective version")
	fl.IntVar(&l.objectiveWindow, "objective-window-seconds", 0, "window the objectives are measured over")
	fl.BoolVar(&l.enforced, "enforced", false, "refuse on exceed rather than only counting (shadow mode)")
}

func (l *servingLimits) create(cmd *cobra.Command, endpoint string) api.ServingQuotaCreate {
	req := api.ServingQuotaCreate{Endpoint: endpoint}
	changed := cmd.Flags().Changed
	if changed("committed-rpm") {
		req.CommittedRPM = &l.rpm
	}
	if changed("committed-tpm") {
		req.CommittedTPM = &l.tpm
	}
	if changed("max-concurrent") {
		req.MaxConcurrentRequests = &l.concurrent
	}
	if changed("max-output-tokens") {
		req.MaxOutputTokens = &l.maxOutput
	}
	if changed("burst-rpm") {
		req.BurstRPM = &l.burst
	}
	if changed("ramp-up-seconds") {
		req.RampUpSeconds = &l.rampUp
	}
	if changed("queue-timeout-ms") {
		req.QueueTimeoutMS = &l.queueTimeout
	}
	if changed("success-rate-objective") {
		req.SuccessRateObjective = &l.successRate
	}
	if changed("objective-window-seconds") {
		req.ObjectiveWindowSeconds = &l.objectiveWindow
	}
	if changed("on-exceed") {
		req.OnExceed = l.onExceed
	}
	if changed("scope") {
		req.LimitScope = l.scope
	}
	if changed("availability-objective") {
		req.AvailabilityObjective = l.availability
	}
	if changed("enforced") {
		req.Enforced = &l.enforced
	}
	return req
}

func (l *servingLimits) update(cmd *cobra.Command) api.ServingQuotaUpdate {
	c := l.create(cmd, "")
	return api.ServingQuotaUpdate{
		CommittedRPM: c.CommittedRPM, CommittedTPM: c.CommittedTPM,
		MaxConcurrentRequests: c.MaxConcurrentRequests, MaxOutputTokens: c.MaxOutputTokens,
		BurstRPM: c.BurstRPM, RampUpSeconds: c.RampUpSeconds, OnExceed: c.OnExceed,
		QueueTimeoutMS: c.QueueTimeoutMS, LimitScope: c.LimitScope,
		SuccessRateObjective: c.SuccessRateObjective, AvailabilityObjective: c.AvailabilityObjective,
		ObjectiveWindowSeconds: c.ObjectiveWindowSeconds, Enforced: c.Enforced,
	}
}

func newServingQuotaCreateCmd(app *App) *cobra.Command {
	limits := &servingLimits{}
	cmd := &cobra.Command{
		Use:     "create <endpoint>",
		Aliases: []string{"new"},
		Short:   "Commit limits on an endpoint that has none",
		Long: `Commit a serving quota on an endpoint. An endpoint carries at most one, so
this is a create and 'hgr serving-quota set' is how it is changed afterwards.

Every limit left unset stays uncommitted, which for an endpoint with no quota
means no ceiling at all on that axis. --enforced is off by default: a fresh
commitment starts in shadow mode, counted and reported but never refused, so it
can be watched before it bites.`,
		Example: `  hgr serving-quota create chat --committed-rpm 1000 --committed-tpm 10000000
  hgr serving-quota create chat --max-concurrent 256 --on-exceed queue --queue-timeout-ms 5000 --enforced`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			endpoint, err := endpointNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			quota, err := app.Backend.CreateServingQuota(ctx, limits.create(cmd, endpoint.ID))
			if err != nil {
				return err
			}
			return app.emitServingQuota(cmd, quota)
		},
	}
	limits.bind(cmd)
	return cmd
}

func newServingQuotaSetCmd(app *App) *cobra.Command {
	limits := &servingLimits{}
	cmd := &cobra.Command{
		Use:     "set <endpoint|quota-id>",
		Aliases: []string{"update", "edit"},
		Short:   "Change the limits on an endpoint's quota",
		Long: `Edit one endpoint's serving quota. Only the axes named are changed; the rest
keep the values they had.

An axis cannot be returned to uncommitted here — that is a different intent, and
'hgr serving-quota rm' removes the commitment entirely when there should be no
ceiling at all.`,
		Example: `  hgr serving-quota set chat --committed-rpm 2000
  hgr serving-quota set chat --enforced`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if cmd.Flags().NFlag() == 0 {
				return errs.New(errs.CodeInputRequired, "set needs at least one limit to change").
					WithFix("See what it has", "hgr serving-quota get "+args[0])
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			quota, err := servingQuotaNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			updated, err := app.Backend.UpdateServingQuota(ctx, quota.ID, limits.update(cmd))
			if err != nil {
				return err
			}
			return app.emitServingQuota(cmd, updated)
		},
	}
	limits.bind(cmd)
	return cmd
}

func newServingQuotaDeleteCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "rm <endpoint|quota-id>",
		Aliases: []string{"delete"},
		Short:   "Remove an endpoint's committed limits entirely",
		Long: `Delete a serving quota. The endpoint keeps serving; it is simply no longer
rate limited, because an endpoint with no quota is uncommitted on every axis.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			quota, err := servingQuotaNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			if err := app.Confirm(fmt.Sprintf("remove the serving quota on %s?", quota.EndpointName)); err != nil {
				return err
			}
			if err := app.Backend.DeleteServingQuota(ctx, quota.ID); err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Names:   []string{quota.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s is no longer rate limited\n", s.Bold(quota.EndpointName))
				},
			})
		},
	}
}

func newServingQuotaResolvedCmd(app *App) *cobra.Command {
	var (
		replicas int
		index    int
		workload string
	)
	cmd := &cobra.Command{
		Use:   "resolved <endpoint>",
		Short: "How the committed limits divide across replicas",
		Long: `Show what one replica of an endpoint actually enforces.

A per_replica limit is the same on every replica; a divided one is split across
them, so what a single replica may serve depends on how many there are. This is
the arithmetic the gateway does, asked of the plane rather than reproduced here.`,
		Example: `  hgr serving-quota resolved chat --replicas 4
  hgr serving-quota resolved chat --replicas 4 --index 1`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			endpoint, err := endpointNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			table, err := app.Backend.ResolvedServingQuotas(ctx, endpoint.ID, workload, replicas, index)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    table,
				Human: func(w io.Writer, s ui.Style, width int) {
					resolvedServingTable(app, w, s, table)
				},
			})
		},
	}
	fl := cmd.Flags()
	fl.IntVar(&replicas, "replicas", 1, "how many replicas the limits divide across")
	fl.IntVar(&index, "index", 0, "which replica to resolve, when a ramp makes them differ")
	fl.StringVar(&workload, "workload", "", "resolve for one workload behind the endpoint")
	return cmd
}

func resolvedServingTable(app *App, w io.Writer, s ui.Style, table api.ResolvedServingQuotaTable) {
	if len(table.Quotas) == 0 {
		fmt.Fprintf(w, "%s\n", s.Dim("no committed limits to resolve"))
		return
	}
	fmt.Fprintf(w, "%s\n", s.Dim(fmt.Sprintf("%d replica(s) · replica %d", table.Replicas, table.ReplicaIndex)))
	for i, q := range table.Quotas {
		if i > 0 {
			fmt.Fprintln(w)
		}
		fmt.Fprintf(w, "%s %s\n", s.Bold(q.EndpointName), s.Dim(fmt.Sprintf("· %s", q.Project)))
		kv := app.KV()
		kv.Add("declared rpm", committedInt(s, q.Declared.CommittedRPM))
		kv.Add("effective rpm", committedInt(s, q.Effective.CommittedRPM))
		kv.Add("declared tpm", committedInt64(s, q.Declared.CommittedTPM))
		kv.Add("effective tpm", committedInt64(s, q.Effective.CommittedTPM))
		kv.Add("declared concurrent", committedInt(s, q.Declared.MaxConcurrentRequests))
		kv.Add("effective concurrent", committedInt(s, q.Effective.MaxConcurrentRequests))
		kv.Add("scope", q.Effective.LimitScope)
		kv.Add("enforcement", enforcementWord(s, q.Effective.Enforced))
		if q.StarvedReplicas > 0 {
			kv.Add("starved replicas", s.Amber(strconv.Itoa(q.StarvedReplicas)))
		}
		kv.Render(w)
	}
}

func (a *App) emitServingQuota(cmd *cobra.Command, quota api.ServingQuota) error {
	return a.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    quota,
		Names:   []string{quota.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			servingQuotaDetail(a, w, s, quota)
		},
		Next: servingQuotaNoun().nextFor(quota),
	})
}

// --- serving-quota rendering helpers ---------------------------------------

// committedInt draws a nullable limit. A null is "uncommitted", which is not a
// dash (no value yet) and emphatically not 0 (a limit of nothing).
func committedInt(s ui.Style, p *int) string {
	if p == nil {
		return s.Dim("uncommitted")
	}
	return strconv.Itoa(*p)
}

func committedInt64(s ui.Style, p *int64) string {
	if p == nil {
		return s.Dim("uncommitted")
	}
	return strconv.FormatInt(*p, 10)
}

func committedRate(s ui.Style, p *float64) string {
	if p == nil {
		return s.Dim("uncommitted")
	}
	return strconv.FormatFloat(*p, 'g', -1, 64)
}

func queueMS(p *int) string {
	if p == nil {
		return "-"
	}
	return fmt.Sprintf("%dms", *p)
}

// enforcementWord is the one distinction a reader must never miss: an enforced
// limit refuses, a shadow one only counts.
func enforcementWord(s ui.Style, enforced bool) string {
	if enforced {
		return s.Green("enforced")
	}
	return s.Amber("shadow")
}

func standardWord(s ui.Style, std api.ServingQuotaStandard) string {
	if std.Meets {
		return s.Green("meets")
	}
	return s.Amber(fmt.Sprintf("%d short", len(std.Shortfalls)))
}
