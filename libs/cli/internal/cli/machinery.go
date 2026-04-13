package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

func allocationNoun() noun[api.Allocation] {
	return noun[api.Allocation]{
		name:    "allocation",
		aliases: []string{"allocations", "alloc"},
		group:   "machinery",
		short:   "Devices committed together to one workload",
		long: `An allocation is the unit of commitment: a set of devices held together
for one workload.

It is observed — placement decides it and the cluster reports it — so there is
nothing to create or delete here. Released allocations stay listed because the
row is how a finished allocation remains auditable; --live narrows to what is
actually held right now.

gang_complete is the question worth asking: holding fewer live devices than
the allocation asked for is a partially satisfied gang, which is a different
situation from being released.`,
		example: `  hgr allocation ls --live
  hgr allocation ls --cluster iad-1 --project core
  hgr allocation ls --device dev_01m0dj…
  hgr allocation ls -o json | jq '.items[] | select(.gang_complete | not)'`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Allocation], error) {
			return b.Allocations(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Allocation, error) {
			return b.Allocation(ctx, ref)
		},
		keys:   func(a api.Allocation) []string { return []string{a.ID} },
		empty:  "no allocations match",
		getArg: "<allocation-id>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "cluster"},
			{Title: "project"},
			{Title: "workload", Wide: true},
			{Title: "class"},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "phase"},
			{Title: "live"},
			{Title: "gang"},
			{Title: "over quota"},
			{Title: "held", Align: ui.AlignRight},
		},
		row: func(s ui.Style, a api.Allocation) []string {
			return []string{
				a.ID, a.ClusterName, a.ProjectSlug, a.Workload, a.DeviceClass,
				fmt.Sprintf("%d/%d", liveDevices(a), a.DeviceCount),
				phaseColor(s, a.Phase), boolWord(a.Live), gangCell(s, a),
				warnIf(s, a.OverQuota), ago(a.AcquiredAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.class, "class", "", "filter by device class")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by phase")
			cmd.Flags().StringVar(&f.replica, "replica", "", "allocations held by one replica")
			cmd.Flags().StringVar(&f.device, "device", "", "which allocation holds one device")
			cmd.Flags().BoolVar(&f.live, "live", false, "only live allocations (--live=false for released)")
			cmd.Flags().BoolVar(&f.overQuota, "over-quota", false, "only allocations above the quota floor")
		},
		detail: func(app *App, w io.Writer, s ui.Style, a api.Allocation) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(a.ID), s.Dim(a.Workload))
			kv := app.KV()
			kv.Add("cluster", a.ClusterName)
			kv.Add("project", a.ProjectSlug)
			kv.Add("replica", orDash(str(a.Replica)))
			kv.Add("device class", a.DeviceClass)
			kv.Addf("devices", "%d requested · %d live", a.DeviceCount, liveDevices(a))
			kv.Add("phase", phaseColor(s, a.Phase))
			kv.Add("live", yesNo(s, a.Live))
			kv.Add("gang complete", yesNo(s, a.GangComplete))
			kv.Add("over quota", warnIf(s, a.OverQuota))
			kv.Add("acquired", ago(a.AcquiredAt))
			kv.Add("released", ago(a.ReleasedAt))
			kv.Add("observed", ago(a.ObservedAt))
			kv.Render(w)

			if len(a.Devices) == 0 {
				return
			}
			fmt.Fprintf(w, "\n%s\n", s.Dim("devices"))
			t := app.Table(
				ui.Column{Title: "device"},
				ui.Column{Title: "uuid", Wide: true},
				ui.Column{Title: "node"},
				ui.Column{Title: "live"},
				ui.Column{Title: "released", Align: ui.AlignRight},
			)
			for _, d := range a.Devices {
				t.Row(d.Device, d.DeviceUUID, d.Node, boolWord(d.Live), ago(d.ReleasedAt))
			}
			t.Render(w)
		},
	}
}

func liveDevices(a api.Allocation) int {
	n := 0
	for _, d := range a.Devices {
		if d.Live {
			n++
		}
	}
	return n
}

func gangCell(s ui.Style, a api.Allocation) string {
	if a.GangComplete {
		return s.Green("complete")
	}
	return s.Amber("partial")
}

func componentNoun() noun[api.Component] {
	return noun[api.Component]{
		name:    "component",
		aliases: []string{"components"},
		group:   "machinery",
		short:   "Declared cluster software versions and observed rollout",
		long: `A component pairs the version we asked a cluster to run with the version
it reports running.

They are a pair and never one nullable column, and behind is derived by the
plane so nobody has to decide locally what it means. A component nobody has
reported on counts as behind: silence is not evidence of a finished rollout.`,
		example: `  hgr component ls --behind
  hgr component ls --cluster iad-1
  hgr component get scheduler`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Component], error) {
			return b.Components(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Component, error) {
			return b.Component(ctx, ref)
		},
		keys:  func(c api.Component) []string { return []string{c.Name, c.ID} },
		empty: "no components declared",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "cluster", Wide: true},
			{Title: "kind"},
			{Title: "desired"},
			{Title: "observed"},
			{Title: "rollout"},
			{Title: "healthy"},
			{Title: "behind"},
			{Title: "seen", Align: ui.AlignRight},
		},
		row: func(s ui.Style, c api.Component) []string {
			return []string{
				c.Name, c.ID, c.ClusterID, c.Kind, c.DesiredVersion,
				orDash(str(c.Status.ObservedVersion)), phaseColor(s, c.Status.RolloutPhase),
				yesNo(s, c.Status.Healthy), warnIf(s, c.Behind), ago(c.Status.ObservedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.name, "name", "", "filter by component name")
			cmd.Flags().StringVar(&f.kind, "kind", "", "filter by component kind")
			cmd.Flags().BoolVar(&f.behind, "behind", false, "only components behind their desired version")
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled components")
			f.bindSearch(cmd, "substring match on name or kind")
		},
		detail: func(app *App, w io.Writer, s ui.Style, c api.Component) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(c.Name), s.Dim(c.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("cluster", c.ClusterID)
			kv.Add("kind", c.Kind)
			kv.Add("desired version", c.DesiredVersion)
			kv.Add("config", labelString(c.Config))
			kv.Add("enabled", yesNo(s, c.Enabled))
			kv.Add("revision", c.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			okv := app.KV()
			okv.Add("observed version", orDash(str(c.Status.ObservedVersion)))
			okv.Add("rollout phase", phaseColor(s, c.Status.RolloutPhase))
			okv.Add("healthy", yesNo(s, c.Status.Healthy))
			okv.Add("message", orDash(str(c.Status.Message)))
			okv.Add("last report", ago(c.Status.ObservedAt))
			okv.Add("behind", warnIf(s, c.Behind))
			okv.Render(w)
		},
	}
}

func eventNoun() noun[api.Event] {
	return noun[api.Event]{
		name:    "event",
		aliases: []string{"events"},
		group:   "machinery",
		short:   "Append-only record of what happened",
		long: `Events are observed and append-only: nothing declares one and nothing may
edit one, so they carry no revision.

They are the first place to look when something changed and nobody remembers
why. --since accepts a duration (30m) or an RFC 3339 timestamp.`,
		example: `  hgr event ls --since 30m
  hgr event ls --cluster iad-1 --severity error
  hgr event ls --kind device.unhealthy --all`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Event], error) {
			return b.Events(ctx, o)
		},
		get:    func(ctx context.Context, b api.Backend, ref string) (api.Event, error) { return b.Event(ctx, ref) },
		keys:   func(e api.Event) []string { return []string{e.ID} },
		empty:  "no events match",
		getArg: "<event-id>",
		columns: []ui.Column{
			{Title: "occurred", Align: ui.AlignRight},
			{Title: "severity"},
			{Title: "kind"},
			{Title: "subject"},
			{Title: "message"},
			{Title: "cluster", Wide: true},
			{Title: "id", Wide: true},
		},
		row: func(s ui.Style, e api.Event) []string {
			return []string{
				agoAt(e.OccurredAt), severityColor(s, e.Severity), e.Kind,
				e.Subject.Type, e.Message, orDash(str(e.ClusterID)), e.ID,
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.subject, "subject", "", "events about one subject (row id)")
			cmd.Flags().StringVar(&f.kind, "kind", "", "filter by event kind")
			cmd.Flags().StringVar(&f.severity, "severity", "", "filter by severity: info|warning|error")
			cmd.Flags().StringVar(&f.since, "since", "", "only events after a duration ago or an RFC 3339 time")
			cmd.Flags().StringVar(&f.until, "until", "", "only events before a duration ago or an RFC 3339 time")
		},
		detail: func(app *App, w io.Writer, s ui.Style, e api.Event) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(e.Kind), s.Dim(e.ID))
			kv := app.KV()
			kv.Add("severity", severityColor(s, e.Severity))
			kv.Add("message", e.Message)
			kv.Addf("subject", "%s %s", e.Subject.Type, orDash(str(e.Subject.ID)))
			kv.Add("cluster", orDash(str(e.ClusterID)))
			kv.Add("operation", orDash(str(e.OperationID)))
			kv.Add("details", labelString(e.Details))
			kv.Addf("occurred", "%s ago", agoAt(e.OccurredAt))
			kv.Addf("recorded", "%s ago", agoAt(e.RecordedAt))
			kv.Render(w)
		},
		next: func(e api.Event) []errs.Action {
			if e.OperationID == nil {
				return nil
			}
			return []errs.Action{{Description: "The operation behind it", Command: "hgr operation get " + *e.OperationID}}
		},
	}
}

func operationNoun() noun[api.Operation] {
	return noun[api.Operation]{
		name:    "operation",
		aliases: []string{"operations", "op"},
		group:   "machinery",
		short:   "Long-running imperative work: drain, reset, warm, roll",
		long: `An operation is the imperative surface, and the only one.

There is no verb endpoint anywhere — no POST on a node's own path — because a
call that returns 200 cannot express who asked, when, why, how many attempts
it has made, or that it is still going. Everything imperative is created here
with a type and a target, and then polled.`,
		example: `  hgr operation ls --phase running
  hgr operation create node.drain iad-1-n01 --reason firmware --wait
  hgr operation get opr_01m0dj…
  hgr operation cancel opr_01m0dj…`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Operation], error) {
			return b.Operations(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Operation, error) {
			return b.Operation(ctx, ref)
		},
		keys:   func(o api.Operation) []string { return []string{o.ID} },
		empty:  "no operations match",
		getArg: "<operation-id>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "type"},
			{Title: "target"},
			{Title: "phase"},
			{Title: "attempts", Align: ui.AlignRight},
			{Title: "actor"},
			{Title: "reason"},
			{Title: "cluster", Wide: true},
			{Title: "age", Align: ui.AlignRight},
			{Title: "took", Align: ui.AlignRight},
		},
		row: func(s ui.Style, o api.Operation) []string {
			return []string{
				o.ID, o.Type, o.Target.Type + " " + shorten(o.Target.ID, 18), phaseColor(s, o.Phase),
				strconv.Itoa(o.Attempts), o.RequestedBy, orDash(str(o.Reason)),
				orDash(str(o.ClusterID)), agoAt(o.CreatedAt), duration(o),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.typ, "type", "", "filter by operation type, e.g. node.drain")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by phase: pending|running|succeeded|failed|cancelled")
			cmd.Flags().StringVar(&f.target, "target", "", "operations against one target")
			cmd.Flags().StringVar(&f.actor, "actor", "", "operations requested by one actor")
			cmd.Flags().StringVar(&f.since, "since", "", "only operations created after a duration ago or an RFC 3339 time")
		},
		detail: func(app *App, w io.Writer, s ui.Style, o api.Operation) {
			operationDetail(app, w, s, o)
		},
		next: func(o api.Operation) []errs.Action {
			if o.Terminal() {
				return []errs.Action{{Description: "What it emitted", Command: "hgr event ls --kind " + o.Type}}
			}
			return []errs.Action{
				{Description: "Wait for it", Command: "hgr operation wait " + o.ID},
				{Description: "Cancel it", Command: "hgr operation cancel " + o.ID},
			}
		},
	}
}

func operationDetail(app *App, w io.Writer, s ui.Style, o api.Operation) {
	fmt.Fprintf(w, "%s %s\n", s.Bold(o.Type), s.Dim(o.ID))

	fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
	kv := app.KV()
	kv.Addf("target", "%s %s", o.Target.Type, o.Target.ID)
	kv.Add("cluster", orDash(str(o.ClusterID)))
	kv.Add("reason", orDash(str(o.Reason)))
	kv.Add("requested by", o.RequestedBy)
	kv.Add("parameters", labelString(o.Parameters))
	kv.Add("deadline", ago(o.DeadlineAt))
	kv.Addf("created", "%s ago", agoAt(o.CreatedAt))
	kv.Render(w)

	fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
	okv := app.KV()
	okv.Add("phase", phaseColor(s, o.Phase))
	okv.Addf("attempts", "%d", o.Attempts)
	okv.Add("started", ago(o.StartedAt))
	okv.Add("finished", ago(o.FinishedAt))
	okv.Add("took", duration(o))
	okv.Add("cancel requested", ago(o.CancelRequestedAt))
	okv.Add("error", orDash(str(o.Error)))
	okv.Add("result", labelString(o.Result))
	okv.Render(w)
}

func duration(o api.Operation) string {
	if o.StartedAt == nil {
		return "-"
	}
	end := time.Now().UTC()
	if o.FinishedAt != nil {
		end = *o.FinishedAt
	}
	d := end.Sub(*o.StartedAt)
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	return fmt.Sprintf("%dm", int(d.Minutes()))
}

// newOperationCmd is the one noun with verbs beyond ls and get, because it is
// the one noun that is a verb.
func newOperationCmd(app *App) *cobra.Command {
	cmd := operationNoun().command(app)
	cmd.AddCommand(newOperationCreateCmd(app), newOperationCancelCmd(app), newOperationWaitCmd(app))
	return cmd
}

func newOperationCreateCmd(app *App) *cobra.Command {
	var (
		reason         string
		deadline       string
		params         []string
		cluster        string
		idempotencyKey string
		wait           bool
		interval       string
	)
	cmd := &cobra.Command{
		Use:     "create <type> <target>",
		Aliases: []string{"new", "start"},
		Short:   "Start an operation against a target",
		Long: `Create an operation: a type and a target, never a verb on the target.

The cluster is derived from the target for anything that carries one, because
two ways to say which cluster is two ways for them to disagree. --cluster is
accepted only for targets that do not, and it is also what tells two objects of
the same name in different clusters apart.

A target is an id or a name. A bare name is qualified with the kind the type
acts upon before it is sent, because a target reference has to say what kind of
thing it names — that is what makes node.drain against a device an error here
rather than inside the cluster an hour later.`,
		Example: `  hgr operation create node.drain iad-1-n01 --reason firmware
  hgr operation create device.reset dev_01m0dj… --reason ecc --wait
  hgr operation create cache.warm artifact:llama-3-70b --cluster iad-1`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			parameters := map[string]any{}
			kv, err := api.ParseKV(params, "param")
			if err != nil {
				return err
			}
			for k, v := range kv {
				parameters[k] = v
			}
			ctx, cancel := app.RequestContext()
			defer cancel()

			req := api.OperationRequest{
				Type: args[0], Target: qualifyOperationTarget(args[0], args[1]), Reason: reason,
				Parameters: parameters, Cluster: cluster,
			}
			if deadline != "" {
				at, err := parseTime(deadline, "deadline")
				if err != nil {
					return err
				}
				req.Deadline = at
			}
			if err := app.Confirm(fmt.Sprintf("start %s on %s?", args[0], args[1])); err != nil {
				return err
			}
			op, err := app.Backend.CreateOperation(ctx, req, idempotencyKey)
			if err != nil {
				return err
			}
			if !wait {
				return app.emitOperation(cmd, op)
			}
			every, err := pollInterval(interval)
			if err != nil {
				return err
			}
			return app.waitForOperation(cmd, op.ID, every)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&reason, "reason", "", "why this is being done — it outlives the person who ran it")
	fl.StringVar(&deadline, "deadline", "", "give up after a duration or at an RFC 3339 time")
	fl.StringArrayVar(&params, "param", nil, "operation parameter (KEY=VALUE, repeatable)")
	fl.StringVar(&cluster, "cluster", "", "cluster, for targets that do not carry one")
	fl.StringVar(&idempotencyKey, "idempotency-key", "", "retry safely: the same key returns the same operation")
	fl.BoolVar(&wait, "wait", false, "poll until the operation reaches a terminal phase")
	fl.StringVar(&interval, "interval", "2s", "how often to poll while waiting")
	return cmd
}

func newOperationCancelCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:   "cancel <operation-id>",
		Short: "Ask an operation to stop",
		Long: `Cancellation, and deliberately not deletion.

Deleting the row would throw away who asked for this and why, which is the
reason the object exists. A pending operation stops outright; a running one is
marked and reaches its terminal phase when the cluster reports it.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			if err := app.Confirm("cancel operation " + args[0] + "?"); err != nil {
				return err
			}
			op, err := app.Backend.CancelOperation(ctx, args[0])
			if err != nil {
				return err
			}
			return app.emitOperation(cmd, op)
		},
	}
}

func newOperationWaitCmd(app *App) *cobra.Command {
	var (
		interval string
		timeout  string
	)
	cmd := &cobra.Command{
		Use:   "wait <operation-id>",
		Short: "Poll an operation until it finishes",
		Long: `Polling is the whole point of an operation having an id.

A drain is the case that matters: it is slow, it can stall, and the answers to
"is it moving" and "why did it stop" are on the object rather than in a log.
Exits non-zero if the operation ends in anything but success.`,
		Example: `  hgr operation wait opr_01m0dj…
  hgr operation wait opr_01m0dj… --interval 5s --timeout 30m`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			every, err := pollInterval(interval)
			if err != nil {
				return err
			}
			if timeout != "" {
				d, err := api.ParseDuration(timeout)
				if err != nil {
					return err
				}
				app.Timeout = d
			}
			return app.waitForOperation(cmd, args[0], every)
		},
	}
	cmd.Flags().StringVar(&interval, "interval", "2s", "how often to poll")
	cmd.Flags().StringVar(&timeout, "timeout", "", "give up after this long (default: the global --timeout)")
	return cmd
}

func pollInterval(raw string) (time.Duration, error) {
	d, err := api.ParseDuration(raw)
	if err != nil {
		return 0, err
	}
	if d < 250*time.Millisecond {
		return 0, errs.New(errs.CodeUsage, "--interval must be at least 250ms")
	}
	return d, nil
}

// waitForOperation polls one operation to a terminal phase. It reports each
// phase change rather than every poll, so a slow drain prints a line when
// something actually happened.
func (a *App) waitForOperation(cmd *cobra.Command, ref string, every time.Duration) error {
	ctx, cancel := a.RequestContext()
	defer cancel()

	step := a.Report.Step("waiting for " + ref)
	last := ""
	for {
		op, err := a.Backend.Operation(ctx, ref)
		if err != nil {
			step.Fail("failed")
			return err
		}
		if op.Phase != last {
			step.Update("%s · %s", op.Type, op.Phase)
			last = op.Phase
		}
		if op.Terminal() {
			if op.Phase == api.OpSucceeded {
				step.Done("%s %s", op.Type, op.Phase)
				return a.emitOperation(cmd, op)
			}
			step.Fail("%s %s", op.Type, op.Phase)
			e := errs.New(errs.CodeInvalidState, "operation %s ended as %s", op.ID, op.Phase)
			if op.Error != nil {
				e = e.WithCause(*op.Error)
			}
			return e.WithFix("Inspect it", "hgr operation get "+op.ID)
		}
		select {
		case <-ctx.Done():
			step.Fail("timed out")
			return errs.New(errs.CodeTimeout, "operation %s was still %s after %s", op.ID, op.Phase, a.Timeout).
				WithFix("Keep watching", "hgr operation wait "+op.ID)
		case <-time.After(every):
		}
	}
}

func (a *App) emitOperation(cmd *cobra.Command, op api.Operation) error {
	return a.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    op,
		Names:   []string{op.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			operationDetail(a, w, s, op)
		},
		Next: operationNoun().nextFor(op),
	})
}

// operationTargetKinds names the kind of object each operation type acts upon.
// It mirrors the plane's own table and exists for one reason: a name carries
// nothing about its own kind, so the kind is written beside it before the
// request leaves. `cache.warm` acts on an artifact, not on the cluster it
// warms — that one is named by --cluster.
var operationTargetKinds = map[string]string{
	"node.drain":        "node",
	"node.cordon":       "node",
	"node.uncordon":     "node",
	"node.reboot":       "node",
	"device.reset":      "device",
	"cache.warm":        "artifact",
	"deployment.roll":   "deployment",
	"component.upgrade": "component",
}

// qualifyOperationTarget turns a human reference into a form the plane can read
// the kind off. An id already carries it in the prefix and an already-qualified
// `node:gpu-node-04` is passed through; a bare name has the type's own target
// kind written in front of it. An unrecognised type is left alone so the plane
// answers for it, listing the types it knows.
func qualifyOperationTarget(opType, ref string) string {
	if isPublicID(ref) || strings.Contains(ref, ":") {
		return ref
	}
	kind, ok := operationTargetKinds[opType]
	if !ok {
		return ref
	}
	return kind + ":" + ref
}

// isPublicID reports whether a reference is already one of the plane's
// prefixed identifiers rather than a name.
func isPublicID(ref string) bool {
	prefix, rest, ok := strings.Cut(ref, "_")
	return ok && len(prefix) == 3 && len(rest) == 26
}
