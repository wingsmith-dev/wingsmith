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

func alertThresholdNoun() noun[api.AlertThreshold] {
	return noun[api.AlertThreshold]{
		name:    "alert-threshold",
		aliases: []string{"alert-thresholds", "threshold", "thresholds"},
		group:   "machinery",
		short:   "Metric thresholds that raise an alert",
		long: `An alert threshold is one rule, a subject to watch, and the value the metric
must cross before it fires.

The metric and which way it is wrong are the rule's, not the caller's: a rule
watches time-to-first-token going up or output speed going down, and 'hgr
alert-threshold rules' is the catalogue the plane will take. A threshold with no
subject is the fleet default for its rule: the line every subject of that type
is held to unless one carries its own.`,
		example: `  hgr alert-threshold ls
  hgr alert-threshold ls --rule deployment.ttft_high
  hgr alert-threshold rules
  hgr alert-threshold create deployment.ttft_high --value 2.5 --subject dep_01m…`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.AlertThreshold], error) {
			return b.AlertThresholds(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.AlertThreshold, error) {
			return b.AlertThreshold(ctx, ref)
		},
		keys:   func(t api.AlertThreshold) []string { return []string{t.ID} },
		empty:  "no alert thresholds declared",
		getArg: "<threshold-id>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "rule"},
			{Title: "subject"},
			{Title: "comparison"},
			{Title: "value", Align: ui.AlignRight},
			{Title: "for", Align: ui.AlignRight},
			{Title: "severity"},
			{Title: "enabled"},
		},
		row: func(s ui.Style, t api.AlertThreshold) []string {
			return []string{
				t.ID, t.Rule, alertSubjectCell(s, t.Subject), t.Comparison,
				trimFloat(t.Value), forCell(t.ForSeconds), orDash(str(t.Severity)),
				yesNo(s, t.Enabled),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.rule, "rule", "", "limit to one rule")
			cmd.Flags().StringVar(&f.kind, "subject-type", "", "filter by subject type: deployment|node")
			cmd.Flags().StringVar(&f.subject, "subject", "", "thresholds watching one subject")
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled thresholds")
			cmd.Flags().BoolVar(&f.fleetDefault, "fleet-default", false, "only the fleet defaults (no subject)")
		},
		detail: alertThresholdDetail,
		next: func(t api.AlertThreshold) []errs.Action {
			return []errs.Action{
				{Description: "Edit its value", Command: "hgr alert-threshold set " + t.ID + " --value N"},
				{Description: "The rule it uses", Command: "hgr alert-threshold rules"},
			}
		},
		extra: func(app *App) []*cobra.Command {
			return []*cobra.Command{
				newAlertThresholdCreateCmd(app),
				newAlertThresholdSetCmd(app),
				newAlertThresholdDeleteCmd(app),
				newAlertRulesCmd(app),
			}
		},
	}
}

func alertThresholdDetail(app *App, w io.Writer, s ui.Style, t api.AlertThreshold) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(t.Rule), s.Dim(t.ID))

	kv := app.KV()
	kv.Add("metric", t.Metric)
	kv.Addf("fires when", "%s %s %s", t.Metric, t.Comparison, trimFloat(t.Value))
	kv.Add("subject", alertSubjectCell(s, t.Subject))
	kv.Add("dimensions", labelString(t.Dimensions))
	kv.Add("for", forCell(t.ForSeconds))
	kv.Add("lookback", orDash(str(t.Lookback)))
	kv.Add("objective", orDash(str(t.Objective)))
	kv.Add("severity", orDash(str(t.Severity)))
	kv.Add("enabled", yesNo(s, t.Enabled))
	kv.Add("reason", orDash(str(t.Reason)))
	kv.Add("created", agoAt(t.CreatedAt))
	kv.Render(w)
}

func newAlertThresholdCreateCmd(app *App) *cobra.Command {
	var (
		value       float64
		subjectType string
		subject     string
		dimensions  []string
		objective   string
		lookback    string
		forSeconds  int
		severity    string
		enabled     bool
		reason      string
	)
	cmd := &cobra.Command{
		Use:     "create <rule>",
		Aliases: []string{"new"},
		Short:   "Declare a threshold on a metric rule",
		Long: `Create an alert threshold for one rule.

The rule names the metric and which direction is wrong; --value is the line it
must cross. Omit --subject to set the fleet default for the rule's subject type;
name one to hold a single deployment or node to a different line. --for is how
long the metric must stay across the line before it fires, so a brief spike
does not.`,
		Example: `  hgr alert-threshold create deployment.ttft_high --value 2.5
  hgr alert-threshold create deployment.ttft_high --value 3 --subject dep_01m… --for 300 --severity error`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !cmd.Flags().Changed("value") {
				return errs.New(errs.CodeInputRequired, "a threshold needs a --value to cross").
					WithFix("See the rule's suggested value", "hgr alert-threshold rules")
			}
			dims, err := parseDimensions(dimensions)
			if err != nil {
				return err
			}
			req := api.AlertThresholdCreate{
				Rule: args[0], Value: value, SubjectType: subjectType, SubjectID: subject,
				Dimensions: dims, Reason: reason,
			}
			if cmd.Flags().Changed("objective") {
				req.Objective = &objective
			}
			if cmd.Flags().Changed("lookback") {
				req.Lookback = &lookback
			}
			if cmd.Flags().Changed("for") {
				req.ForSeconds = &forSeconds
			}
			if cmd.Flags().Changed("severity") {
				req.Severity = &severity
			}
			if cmd.Flags().Changed("enabled") {
				req.Enabled = &enabled
			}

			ctx, cancel := app.RequestContext()
			defer cancel()
			threshold, err := app.Backend.CreateAlertThreshold(ctx, req)
			if err != nil {
				return err
			}
			return app.emitAlertThreshold(cmd, threshold)
		},
	}
	fl := cmd.Flags()
	fl.Float64Var(&value, "value", 0, "the line the metric must cross")
	fl.StringVar(&subjectType, "subject-type", "", "subject type, defaulting to the rule's")
	fl.StringVar(&subject, "subject", "", "one subject to watch (omit for the fleet default)")
	fl.StringArrayVar(&dimensions, "dimension", nil, "narrow the metric (KEY=VALUE, repeatable)")
	fl.StringVar(&objective, "objective", "", "objective version this grades against")
	fl.StringVar(&lookback, "lookback", "", "window the metric is read over, e.g. 5m")
	fl.IntVar(&forSeconds, "for", 0, "seconds the metric must stay across before firing")
	fl.StringVar(&severity, "severity", "", "info|warning|error, defaulting to the rule's")
	fl.BoolVar(&enabled, "enabled", true, "whether it fires (--enabled=false to declare it dormant)")
	fl.StringVar(&reason, "reason", "", "why this line was drawn: it outlives whoever drew it")
	return cmd
}

func newAlertThresholdSetCmd(app *App) *cobra.Command {
	var (
		value      float64
		dimensions []string
		objective  string
		lookback   string
		forSeconds int
		severity   string
		enabled    bool
		reason     string
	)
	cmd := &cobra.Command{
		Use:     "set <threshold-id>",
		Aliases: []string{"update", "edit"},
		Short:   "Change a threshold's value or window",
		Long: `Edit an alert threshold. Only the fields named change; the rule, metric and
subject are its identity and are not among them: a different rule or subject is
a different threshold.`,
		Example: `  hgr alert-threshold set alt_01m… --value 3
  hgr alert-threshold set alt_01m… --enabled=false`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if cmd.Flags().NFlag() == 0 {
				return errs.New(errs.CodeInputRequired, "set needs at least one field to change").
					WithFix("See what it has", "hgr alert-threshold get "+args[0])
			}
			dims, err := parseDimensions(dimensions)
			if err != nil {
				return err
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			threshold, err := alertThresholdNoun().resolveExact(ctx, app, args[0])
			if err != nil {
				return err
			}
			var req api.AlertThresholdUpdate
			if cmd.Flags().Changed("value") {
				req.Value = &value
			}
			if cmd.Flags().Changed("dimension") {
				req.Dimensions = dims
			}
			if cmd.Flags().Changed("objective") {
				req.Objective = &objective
			}
			if cmd.Flags().Changed("lookback") {
				req.Lookback = &lookback
			}
			if cmd.Flags().Changed("for") {
				req.ForSeconds = &forSeconds
			}
			if cmd.Flags().Changed("severity") {
				req.Severity = &severity
			}
			if cmd.Flags().Changed("enabled") {
				req.Enabled = &enabled
			}
			if cmd.Flags().Changed("reason") {
				req.Reason = &reason
			}
			updated, err := app.Backend.UpdateAlertThreshold(ctx, threshold.ID, req)
			if err != nil {
				return err
			}
			return app.emitAlertThreshold(cmd, updated)
		},
	}
	fl := cmd.Flags()
	fl.Float64Var(&value, "value", 0, "the line the metric must cross")
	fl.StringArrayVar(&dimensions, "dimension", nil, "narrow the metric (KEY=VALUE, repeatable)")
	fl.StringVar(&objective, "objective", "", "objective version this grades against")
	fl.StringVar(&lookback, "lookback", "", "window the metric is read over, e.g. 5m")
	fl.IntVar(&forSeconds, "for", 0, "seconds the metric must stay across before firing")
	fl.StringVar(&severity, "severity", "", "info|warning|error")
	fl.BoolVar(&enabled, "enabled", true, "whether it fires")
	fl.StringVar(&reason, "reason", "", "why this line was drawn")
	return cmd
}

func newAlertThresholdDeleteCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "rm <threshold-id>",
		Aliases: []string{"delete"},
		Short:   "Remove an alert threshold",
		Long: `Delete an alert threshold. If it was a subject's own line, that subject falls
back to the fleet default for the rule; if it was the fleet default, the rule
stops firing until another is declared.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			threshold, err := alertThresholdNoun().resolveExact(ctx, app, args[0])
			if err != nil {
				return err
			}
			if err := app.Confirm(fmt.Sprintf("delete the %s threshold?", threshold.Rule)); err != nil {
				return err
			}
			if err := app.Backend.DeleteAlertThreshold(ctx, threshold.ID); err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    deleted(threshold.ID, threshold.Rule),
				Names:   []string{threshold.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s threshold deleted\n", s.Bold(threshold.Rule))
				},
			})
		},
	}
}

func newAlertRulesCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "rules",
		Aliases: []string{"catalogue", "catalog"},
		Short:   "The metric rules a threshold may be declared on",
		Long: `List the rules the plane will take a threshold for.

Each names the metric, the unit of its value, and which direction is bad.
This is the catalogue the plane validates --value against.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			rules, err := app.Backend.AlertRules(ctx)
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    rules,
				Human: func(w io.Writer, s ui.Style, width int) {
					t := app.Table(
						ui.Column{Title: "rule"},
						ui.Column{Title: "metric"},
						ui.Column{Title: "subject"},
						ui.Column{Title: "direction"},
						ui.Column{Title: "suggested", Align: ui.AlignRight},
						ui.Column{Title: "unit"},
						ui.Column{Title: "severity"},
						ui.Column{Title: "measures"},
					)
					for _, r := range rules {
						t.Row(r.Name, r.Metric, r.SubjectType, r.Comparison,
							trimFloat(r.SuggestedValue), r.Unit, r.Severity, r.Measures)
					}
					t.Render(w)
				},
			})
		},
	}
}

func (a *App) emitAlertThreshold(cmd *cobra.Command, threshold api.AlertThreshold) error {
	return a.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    threshold,
		Names:   []string{threshold.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			alertThresholdDetail(a, w, s, threshold)
		},
		Next: alertThresholdNoun().nextFor(threshold),
	})
}

// --- alert rendering helpers -----------------------------------------------

// alertSubjectCell names what a threshold watches. A threshold with no subject
// is the fleet default for its type, which is a distinct thing from a dash.
func alertSubjectCell(s ui.Style, subject api.AlertSubject) string {
	if subject.ID == nil || *subject.ID == "" {
		return s.Dim(subject.Type + " (fleet)")
	}
	return subject.Type + " " + *subject.ID
}

func forCell(p *int) string {
	if p == nil {
		return "-"
	}
	return strconv.Itoa(*p) + "s"
}

// parseDimensions turns KEY=VALUE flags into the metric-narrowing map the plane
// takes. Values arrive as strings and are left as strings; a dimension is a
// label on a series, not a typed field.
func parseDimensions(pairs []string) (map[string]any, error) {
	kv, err := api.ParseKV(pairs, "dimension")
	if err != nil {
		return nil, err
	}
	if len(kv) == 0 {
		return nil, nil
	}
	out := map[string]any{}
	for k, v := range kv {
		out[k] = v
	}
	return out, nil
}
