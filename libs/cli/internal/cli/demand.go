package cli

import (
	"context"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

func artifactNoun() noun[api.Artifact] {
	return noun[api.Artifact]{
		name:    "artifact",
		aliases: []string{"artifacts"},
		group:   "demand",
		short:   "Immutable, content-addressed model weights",
		long: `An artifact is bytes, and its identity is the digest.

A fine-tune output, a private checkpoint, a quantized derivative and a LoRA
adapter are all artifacts, which is why kind is a field rather than a
different object. Locations are where the bytes can be fetched from; the
control plane never carries them.`,
		example: `  hgr artifact ls
  hgr artifact ls --kind adapter
  hgr artifact get llama-3.1-70b-instruct`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Artifact], error) {
			return b.Artifacts(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Artifact, error) {
			return b.Artifact(ctx, ref)
		},
		keys:  func(a api.Artifact) []string { return []string{a.Name, a.ID, a.Digest} },
		empty: "no artifacts published",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "kind"},
			{Title: "format"},
			{Title: "size", Align: ui.AlignRight},
			{Title: "params", Align: ui.AlignRight},
			{Title: "locations", Align: ui.AlignRight},
			{Title: "digest", Wide: true},
			{Title: "created", Align: ui.AlignRight},
		},
		row: func(s ui.Style, a api.Artifact) []string {
			return []string{
				a.Name, a.ID, a.Kind, orDash(str(a.Format)), bytesHuman(a.SizeBytes),
				paramCount(a.ParameterCount), strconv.Itoa(len(a.Locations)),
				shorten(a.Digest, 19), agoAt(a.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.kind, "kind", "", "filter by kind, e.g. weights|adapter")
			cmd.Flags().StringVar(&f.format, "format", "", "filter by format, e.g. safetensors")
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			f.bindSearch(cmd, "substring match on name or digest")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, a api.Artifact) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(a.Name), s.Dim(a.ID))
			kv := app.KV()
			kv.Add("digest", a.Digest)
			kv.Add("kind", a.Kind)
			kv.Add("format", orDash(str(a.Format)))
			kv.Add("size", bytesHuman(a.SizeBytes))
			kv.Add("parameters", paramCount(a.ParameterCount))
			kv.Add("licence", orDash(str(a.Licence)))
			kv.Add("base artifact", orDash(str(a.BaseArtifactID)))
			kv.Add("project", orDash(str(a.ProjectID)))
			kv.Add("metadata", labelString(a.Metadata))
			kv.Add("created", agoAt(a.CreatedAt))
			kv.Add("revision", a.Revision)
			kv.Render(w)

			if len(a.Locations) == 0 {
				return
			}
			fmt.Fprintf(w, "\n%s\n", s.Dim("locations"))
			t := app.Table(
				ui.Column{Title: "scheme"},
				ui.Column{Title: "uri"},
				ui.Column{Title: "region"},
				ui.Column{Title: "preferred"},
			)
			for _, l := range a.Locations {
				t.Row(l.Scheme, l.URI, orDash(str(l.Region)), boolWord(l.Preferred))
			}
			t.Render(w)
		},
		next: func(a api.Artifact) []errs.Action {
			return []errs.Action{{Description: "Recipes that serve it", Command: "hgr recipe ls --artifact " + a.ID}}
		},
	}
}

func paramCount(v *int64) string {
	if v == nil {
		return "-"
	}
	switch {
	case *v >= 1_000_000_000:
		return fmt.Sprintf("%.0fB", float64(*v)/1e9)
	case *v >= 1_000_000:
		return fmt.Sprintf("%.0fM", float64(*v)/1e6)
	}
	return strconv.FormatInt(*v, 10)
}

func shorten(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func recipeNoun() noun[api.Recipe] {
	return noun[api.Recipe]{
		name:    "recipe",
		aliases: []string{"recipes"},
		group:   "demand",
		short:   "A reproducible way to serve one artifact",
		long: `A recipe is engine, parallelism, cache configuration and the hardware
shape they imply.

The shape is class-first on purpose: a tensor-parallel-8 recipe means nothing
spread across an A100 and an H100, so a device count without a device class is
not a shape.`,
		example: `  hgr recipe ls
  hgr recipe ls --engine vllm --class h100-80
  hgr recipe get llama-70b-tp8`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Recipe], error) {
			return b.Recipes(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Recipe, error) { return b.Recipe(ctx, ref) },
		keys:  func(r api.Recipe) []string { return []string{r.Name, r.ID} },
		empty: "no recipes defined",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "v", Align: ui.AlignRight},
			{Title: "engine"},
			{Title: "class"},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "tp/pp/ep"},
			{Title: "context", Align: ui.AlignRight},
			{Title: "one domain"},
		},
		row: func(s ui.Style, r api.Recipe) []string {
			return []string{
				r.Name, r.ID, strconv.Itoa(r.Version), r.Engine + " " + r.EngineVersion,
				r.Shape.DeviceClass, strconv.Itoa(r.Shape.DeviceCount),
				fmt.Sprintf("%d/%d/%d", r.Parallelism.Tensor, r.Parallelism.Pipeline, r.Parallelism.Expert),
				strconv.Itoa(r.ContextLength), boolWord(r.Shape.RequiresSingleTopologyDomain),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.artifact, "artifact", "", "limit to one artifact")
			cmd.Flags().StringVar(&f.engine, "engine", "", "filter by engine, e.g. vllm")
			cmd.Flags().StringVar(&f.class, "class", "", "filter by device class")
			f.bindSearch(cmd, "substring match on name or engine")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, r api.Recipe) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(r.Name), s.Dim(r.ID))
			kv := app.KV()
			kv.Add("artifact", r.ArtifactID)
			kv.Addf("version", "%d", r.Version)
			kv.Addf("engine", "%s %s", r.Engine, r.EngineVersion)
			kv.Add("image", orDash(str(r.EngineImage)))
			kv.Add("engine args", labelString(r.EngineArgs))
			kv.Add("quantization", orDash(str(r.Quantization)))
			kv.Add("dtype", orDash(str(r.Dtype)))
			kv.Addf("parallelism", "tensor %d · pipeline %d · expert %d", r.Parallelism.Tensor, r.Parallelism.Pipeline, r.Parallelism.Expert)
			kv.Addf("context", "%d", r.ContextLength)
			kv.Add("max batch tokens", num(r.MaxBatchTokens))
			kv.Add("labels", labelString(r.Labels))
			kv.Add("revision", r.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("shape"))
			skv := app.KV()
			skv.Add("device class", r.Shape.DeviceClass)
			skv.Addf("device count", "%d", r.Shape.DeviceCount)
			skv.Add("min device memory", mib(r.Shape.DeviceMemoryMiBMin))
			skv.Add("single topology domain", yesNo(s, r.Shape.RequiresSingleTopologyDomain))
			skv.Add("domain kind", orDash(str(r.Shape.TopologyDomainKind)))
			skv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("cache and resources"))
			ckv := app.KV()
			ckv.Add("kv cache dtype", orDash(str(r.Cache.KVCacheDtype)))
			ckv.Add("kv cache fraction", fraction(r.Cache.KVCacheFraction))
			ckv.Add("prefix cache", boolWord(r.Cache.PrefixCacheEnabled))
			ckv.Add("cpu", num(r.Resources.CPUCores))
			ckv.Add("memory", mibPtr(r.Resources.MemoryMiB))
			ckv.Add("disk", gib(r.Resources.DiskGiB))
			ckv.Render(w)
		},
		next: func(r api.Recipe) []errs.Action {
			return []errs.Action{{Description: "Where it runs", Command: "hgr deployment ls --recipe " + r.ID}}
		},
	}
}

// authorityWord renders who owns a deployment, and leans on colour to make the difference
// impossible to skim past: managed means this plane writes to the cluster.
//
// An empty value reads as observed rather than as unknown, matching the operator's own gate — a
// deployment from before the field existed is not managed, and showing a dash here would suggest
// the question is open when it is not.
func authorityWord(s ui.Style, v string) string {
	if v == "managed" {
		return s.Bold("managed")
	}
	return s.Dim("observed")
}

func fraction(v *float64) string {
	if v == nil {
		return "-"
	}
	return strconv.FormatFloat(*v, 'g', -1, 64)
}

func gib(v *int) string {
	if v == nil {
		return "-"
	}
	return strconv.Itoa(*v) + "GiB"
}

func deploymentNoun() noun[api.Deployment] {
	return noun[api.Deployment]{
		name:    "deployment",
		aliases: []string{"deployments", "deploy"},
		group:   "demand",
		short:   "A recipe placed on a cluster, with scale bounds",
		long: `A deployment is recipe × cluster: the declared spec, the workload it is a
specialisation of, and the replicas the cluster reports.

The three are kept apart on purpose. The spec is what we asked for, workload
carries the scheduling phase and priority that preemption and quota read, and
replicas is a rollup of what is actually running.`,
		example: `  hgr deployment ls
  hgr deployment ls --cluster iad-1 --phase running
  hgr deployment ls --project core -o wide
  hgr deployment get chat-iad`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Deployment], error) {
			return b.Deployments(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Deployment, error) {
			return b.Deployment(ctx, ref)
		},
		keys:  func(d api.Deployment) []string { return []string{d.Name, d.ID} },
		empty: "no deployments placed",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "cluster", Wide: true},
			{Title: "type"},
			{Title: "phase"},
			{Title: "priority"},
			{Title: "replicas", Align: ui.AlignRight},
			{Title: "desired", Align: ui.AlignRight},
			{Title: "bounds", Align: ui.AlignRight},
			{Title: "gen", Align: ui.AlignRight},
			{Title: "authority"},
			{Title: "waiting on", Wide: true},
			{Title: "adopted"},
			{Title: "enabled"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, d api.Deployment) []string {
			return []string{
				d.Name, d.ID, d.ClusterID, d.Workload.Type, phaseColor(s, d.Workload.Phase),
				d.Workload.PriorityClass,
				fmt.Sprintf("%d/%d", d.Replicas.Ready, d.Replicas.Total),
				strconv.Itoa(d.DesiredReplicas),
				fmt.Sprintf("%d-%d", d.MinReplicas, d.MaxReplicas),
				strconv.Itoa(d.Generation), authorityWord(s, d.Authority), waitingOn(s, d),
				boolWord(d.AdoptedWorkloadID != nil),
				boolWord(d.Enabled), agoAt(d.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.recipe, "recipe", "", "limit to one recipe")
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by workload phase: pending|admitted|placed|running")
			cmd.Flags().StringVar(&f.typ, "type", "", "filter by workload type: service|task|gang")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, d api.Deployment) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(d.Name), s.Dim(d.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("cluster", d.ClusterID)
			kv.Add("recipe", d.RecipeID)
			kv.Addf("replicas", "%d desired · bounds %d-%d", d.DesiredReplicas, d.MinReplicas, d.MaxReplicas)
			kv.Addf("generation", "%d", d.Generation)
			kv.Add("enabled", yesNo(s, d.Enabled))
			kv.Add("authority", authorityWord(s, d.Authority))
			kv.Add("adapters", orDash(strings.Join(d.AdapterArtifactIDs, " ")))
			kv.Add("overrides", labelString(d.Overrides))
			kv.Add("labels", labelString(d.Labels))
			kv.Add("revision", d.Revision)
			kv.Render(w)

			if d.AdoptedWorkloadID != nil {
				fmt.Fprintf(w, "\n%s\n", s.Dim("adopted"))
				akv := app.KV()
				akv.Add("workload", str(d.AdoptedWorkloadID))
				akv.Add("pinned to", shortDigest(str(d.AdoptedFingerprint)))
				akv.Add("note", "the cluster refuses to reconcile this while what is running digests to anything else")
				akv.Render(w)
			}

			fmt.Fprintf(w, "\n%s\n", s.Dim("workload"))
			wkv := app.KV()
			wkv.Add("type", d.Workload.Type)
			wkv.Add("phase", phaseColor(s, d.Workload.Phase))
			wkv.Add("reason", orDash(str(d.Workload.Reason)))
			wkv.Add("project", d.Workload.ProjectID)
			wkv.Add("priority class", d.Workload.PriorityClass)
			wkv.Add("preemptible", boolWord(d.Workload.Preemptible))
			wkv.Addf("gang size", "%d", d.Workload.GangSize)
			wkv.Add("queued", ago(d.Workload.QueuedAt))
			wkv.Add("admitted", ago(d.Workload.AdmittedAt))
			wkv.Add("placed", ago(d.Workload.PlacedAt))
			wkv.Add("started", ago(d.Workload.StartedAt))
			wkv.Add("deadline", ago(d.Workload.DeadlineAt))
			wkv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			if d.Status == nil {
				fmt.Fprintf(w, "  %s\n", s.Dim("no cluster has reported on this deployment yet"))
			} else {
				okv := app.KV()
				okv.Add("phase", phaseColor(s, d.Status.Phase))
				okv.Add("reason", orDash(str(d.Status.Reason)))
				okv.Add("detail", orDash(str(d.Status.Message)))
				okv.Addf("replicas", "%d/%d ready · %d unavailable",
					d.Status.ReadyReplicas, d.Status.DesiredReplicas, d.Status.UnavailableReplicas)
				okv.Addf("generation", "%d observed · %d declared", d.Status.ObservedGeneration, d.Generation)
				if d.Status.LastErrorCode != nil {
					okv.Add("last error", s.Red(str(d.Status.LastErrorCode)+": "+str(d.Status.LastErrorMessage)))
				}
				okv.Add("reported", ago(d.Status.ObservedAt))
				okv.Render(w)
			}

			fmt.Fprintf(w, "\n%s\n", s.Dim("replicas"))
			r := d.Replicas
			t := app.Table(
				ui.Column{Title: "total", Align: ui.AlignRight},
				ui.Column{Title: "ready", Align: ui.AlignRight},
				ui.Column{Title: "running", Align: ui.AlignRight},
				ui.Column{Title: "pending", Align: ui.AlignRight},
				ui.Column{Title: "failed", Align: ui.AlignRight},
				ui.Column{Title: "current gen", Align: ui.AlignRight},
			)
			t.Row(strconv.Itoa(r.Total), strconv.Itoa(r.Ready), strconv.Itoa(r.Running),
				strconv.Itoa(r.Pending), countCell(s, r.Failed), strconv.Itoa(r.AtCurrentGeneration))
			t.Render(w)
		},
		next: func(d api.Deployment) []errs.Action {
			return []errs.Action{
				{Description: "What happened to it", Command: "hgr deployment events " + d.Name},
				{Description: "What holds its devices", Command: "hgr allocation ls --cluster " + d.ClusterID},
				{Description: "Roll it", Command: "hgr operation create deployment.roll " + d.Name},
			}
		},
		extra: func(app *App) []*cobra.Command {
			return []*cobra.Command{newDeploymentEventsCmd(app)}
		},
	}
}

// newDeploymentEventsCmd renders one deployment's whole history, oldest first.
//
// Its own subcommand rather than a section of `get`, because `get` answers "what is
// true now" in a fixed amount of space and this is unbounded. It is also the one
// listing in the CLI that is deliberately oldest-first: every other listing is a set
// where the newest row matters most, and this is a sequence that only reads correctly
// in the order it happened.
func newDeploymentEventsCmd(app *App) *cobra.Command {
	var (
		since    string
		severity string
	)
	cmd := &cobra.Command{
		Use:     "events <deployment>",
		Aliases: []string{"timeline", "history"},
		Short:   "What has happened to one deployment",
		Long: `The whole story of a deployment, from being asked for to serving.

Both halves of it. deployment.created, deployment.scaled and deployment.deleted
are things a person did; deployment.dispatched is the plane saying it delivered
the declared state to a cluster; deployment.pending, deployment.placed and
deployment.failed are what that cluster concluded when it tried to reconcile it.

This is the answer to "it says placed and nothing is happening". The status on
` + "`hgr deployment get`" + ` only ever shows the latest conclusion, so a deployment that
waited an hour on cold weights and one that came up instantly look identical
once they are up.`,
		Example: `  hgr deployment events chat-iad
  hgr deployment events chat-iad --since 1h
  hgr deployment events chat-iad --severity error`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()

			// Resolved through the noun so that a prefix or a name works here exactly
			// as it does on `get`. The events endpoint filters on the deployment's own
			// id and would simply return nothing for anything else.
			d, err := deploymentNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}

			at, err := parseTime(since, "since")
			if err != nil {
				return err
			}
			opts := api.ListOpts{All: true, Subject: d.ID, Since: at, Severity: severity}
			page, err := app.Backend.Events(ctx, opts)
			if err != nil {
				return err
			}

			items := page.Items
			slices.Reverse(items)

			rows := make([][]string, 0, len(items))
			for _, e := range items {
				rows = append(rows, []string{
					agoAt(e.OccurredAt), e.Kind, eventReason(e), e.Message,
				})
			}

			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    items,
				Names:   []string{d.Name},
				Columns: []string{"when", "event", "reason", "detail"},
				Rows:    rows,
				Human: func(w io.Writer, s ui.Style, width int) {
					if len(items) == 0 {
						fmt.Fprintf(w, "%s\n", s.Dim("nothing has been recorded against "+d.Name))
						return
					}
					t := app.Table(
						ui.Column{Title: "when", Align: ui.AlignRight},
						ui.Column{Title: "event"},
						ui.Column{Title: "reason"},
						ui.Column{Title: "detail"},
					)
					for _, e := range items {
						t.Row(agoAt(e.OccurredAt), severityColor(s, e.Kind), eventReason(e), e.Message)
					}
					t.Render(w)
				},
				Next: []errs.Action{
					{Description: "The deployment itself", Command: "hgr deployment get " + d.Name},
				},
			})
		},
	}
	cmd.Flags().StringVar(&since, "since", "", "only events after a duration ago or an RFC 3339 time")
	cmd.Flags().StringVar(&severity, "severity", "", "filter by severity: info|warning|error")
	return cmd
}

// eventReason is the machine-readable half of why a reconcile concluded what it did.
//
// Read off details rather than parsed out of the message, because it is the field the
// operator sets deliberately — WeightsNotResident, NoSchedulableCapacity, Applied —
// and the message around it is prose that may be reworded.
func eventReason(e api.Event) string {
	if reason, ok := e.Details["reason"].(string); ok && reason != "" {
		return reason
	}
	return "-"
}

func endpointNoun() noun[api.Endpoint] {
	return noun[api.Endpoint]{
		name:    "endpoint",
		aliases: []string{"endpoints"},
		group:   "demand",
		short:   "The stable public name clients call",
		long: `An endpoint is the name that does not move.

It spans clusters by construction — that is what the weighted target set and
the failover order are for — so it carries no cluster at any depth. Deployments
behind it are disposable; the endpoint is the contract.`,
		example: `  hgr endpoint ls
  hgr endpoint ls --project core
  hgr endpoint get chat`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Endpoint], error) {
			return b.Endpoints(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Endpoint, error) {
			return b.Endpoint(ctx, ref)
		},
		keys:  func(e api.Endpoint) []string { return []string{e.Name, e.ID} },
		empty: "no endpoints published",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "project", Wide: true},
			{Title: "visibility"},
			{Title: "routing"},
			{Title: "targets", Align: ui.AlignRight},
			{Title: "enabled"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, e api.Endpoint) []string {
			return []string{
				e.Name, e.ID, e.ProjectID, e.Visibility, e.RoutingStrategy,
				fmt.Sprintf("%d/%d", enabledTargets(e), len(e.Targets)),
				boolWord(e.Enabled), agoAt(e.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.artifact, "artifact", "", "endpoints serving one artifact")
			f.bindSearch(cmd, "substring match on name")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, e api.Endpoint) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(e.Name), s.Dim(e.ID))
			kv := app.KV()
			kv.Add("display name", orDash(str(e.DisplayName)))
			kv.Add("project", e.ProjectID)
			kv.Add("visibility", e.Visibility)
			kv.Add("routing", e.RoutingStrategy)
			kv.Add("enabled", yesNo(s, e.Enabled))
			kv.Add("labels", labelString(e.Labels))
			kv.Add("revision", e.Revision)
			kv.Render(w)

			if len(e.Targets) > 0 {
				fmt.Fprintf(w, "\n%s\n", s.Dim("targets"))
				t := app.Table(
					ui.Column{Title: "deployment"},
					ui.Column{Title: "role"},
					ui.Column{Title: "weight", Align: ui.AlignRight},
					ui.Column{Title: "enabled"},
					ui.Column{Title: "failover", Align: ui.AlignRight},
				)
				for _, target := range e.Targets {
					t.Row(target.DeploymentID, target.Role, strconv.Itoa(target.Weight),
						boolWord(target.Enabled), failoverPosition(e, target.DeploymentID))
				}
				t.Render(w)
			}
		},
		next: func(e api.Endpoint) []errs.Action {
			if len(e.Targets) == 0 {
				return nil
			}
			return []errs.Action{{Description: "Its primary deployment", Command: "hgr deployment get " + e.Targets[0].DeploymentID}}
		},
	}
}

func enabledTargets(e api.Endpoint) int {
	n := 0
	for _, t := range e.Targets {
		if t.Enabled {
			n++
		}
	}
	return n
}

func failoverPosition(e api.Endpoint, deployment string) string {
	for i, id := range e.FailoverOrder {
		if id == deployment {
			return strconv.Itoa(i + 1)
		}
	}
	return "-"
}

// waitingOn is the one-cell answer to why a deployment is not serving yet.
//
// The cluster's reason rather than the plane's phase, because the plane's phase says "placed" for
// everything between the decision and the first replica and the reason says which of six orderings
// is actually outstanding. Empty when there is nothing to say: no report yet reads as a dash, and a
// deployment whose replicas are up has no reason at all.
func waitingOn(s ui.Style, d api.Deployment) string {
	if d.Status == nil {
		return "-"
	}
	if d.Status.Reason == nil || *d.Status.Reason == "" {
		return "-"
	}
	if d.Status.Phase == "failed" {
		return s.Red(*d.Status.Reason)
	}
	if d.Status.ReadyReplicas > 0 {
		return "-"
	}
	return s.Amber(*d.Status.Reason)
}
