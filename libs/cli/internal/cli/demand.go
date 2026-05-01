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
			return []errs.Action{{Description: "What runs it", Command: "hgr workload ls --recipe " + r.ID}}
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
		short:   "One workload's share on one cluster",
		long: `A deployment is recipe × cluster: the unit the operator reconciles.

You do not create one. Placement creates it when it expands a workload, one per
cluster the workload landed on, and the replica bounds on it are THIS CLUSTER'S
share of the workload total — never the total. Recipe, adapters, overrides and
labels are workload facts and are read there; admission is here, because a
workload can be running in one region and queued on quota in another.`,
		example: `  hgr deployment ls
  hgr deployment ls --cluster iad-1 --phase placed
  hgr deployment ls --workload chat
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
			{Title: "workload"},
			{Title: "cluster", Wide: true},
			{Title: "admission"},
			{Title: "share", Align: ui.AlignRight},
			{Title: "replicas", Align: ui.AlignRight},
			{Title: "gen", Align: ui.AlignRight},
			{Title: "authority"},
			{Title: "waiting on", Wide: true},
			{Title: "adopted"},
			{Title: "enabled"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, d api.Deployment) []string {
			return []string{
				d.Name, d.ID, orDash(d.WorkloadName), d.ClusterID,
				phaseColor(s, d.AdmissionPhase),
				strconv.Itoa(d.DesiredReplicas),
				fmt.Sprintf("%d/%d", d.Replicas.Ready, d.Replicas.Total),
				strconv.Itoa(d.Generation), authorityWord(s, d.Authority), waitingOn(s, d),
				boolWord(d.AdoptedDiscoveredID != nil),
				boolWord(d.Enabled), agoAt(d.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.workload, "workload", "", "limit to one workload's shares")
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by admission phase: pending|admitted|placed|running")
		},
		groupKey: func(by string, d api.Deployment) (string, string, error) {
			switch by {
			case "cluster":
				return d.ClusterID, "cluster", nil
			case "workload":
				return orDash(d.WorkloadName), "workload", nil
			case "phase":
				return d.AdmissionPhase, "phase", nil
			}
			return "", "", errs.New(errs.CodeUsage, "cannot group deployments by %q", by).
				WithCause("valid groupings: cluster, workload, phase")
		},
		detail: func(app *App, w io.Writer, s ui.Style, d api.Deployment) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(d.Name), s.Dim(d.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("workload", orDash(d.WorkloadName)+" "+d.WorkloadID)
			kv.Add("cluster", d.ClusterID)
			kv.Addf("share", "%d of this workload's total", d.DesiredReplicas)
			kv.Addf("generation", "%d", d.Generation)
			kv.Add("enabled", yesNo(s, d.Enabled))
			kv.Add("authority", authorityWord(s, d.Authority))
			kv.Add("revision", d.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("admission"))
			akv := app.KV()
			akv.Add("phase", phaseColor(s, d.AdmissionPhase))
			akv.Add("reason", orDash(str(d.AdmissionReason)))
			akv.Add("queued", ago(d.QueuedAt))
			akv.Add("admitted", ago(d.AdmittedAt))
			akv.Add("placed", ago(d.PlacedAt))
			akv.Render(w)

			if d.AdoptedDiscoveredID != nil {
				fmt.Fprintf(w, "\n%s\n", s.Dim("adopted"))
				dkv := app.KV()
				dkv.Add("discovered", str(d.AdoptedDiscoveredID))
				dkv.Add("pinned to", shortDigest(str(d.AdoptedFingerprint)))
				dkv.Add("note", "the cluster refuses to reconcile this while what is running digests to anything else")
				dkv.Render(w)
			}

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
				{Description: "The workload it is a share of", Command: "hgr workload get " + d.WorkloadID},
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

// newWorkloadCmd is the noun users actually create, scale and delete, with the
// three long-running changes hung off it as verbs.
//
// Scale, pause and drain are operations rather than field writes: each can sit
// pending on quota indefinitely and each can fail, so they get an id to poll
// rather than a status field to misread.
func newWorkloadCmd(app *App) *cobra.Command {
	cmd := workloadNoun().command(app)
	cmd.AddCommand(
		newWorkloadCreateCmd(app),
		newWorkloadDeleteCmd(app),
		newWorkloadScaleCmd(app),
		newWorkloadPauseCmd(app, true),
		newWorkloadPauseCmd(app, false),
		newWorkloadDrainCmd(app),
	)
	return cmd
}

func workloadNoun() noun[api.Workload] {
	return noun[api.Workload]{
		name:    "workload",
		aliases: []string{"workloads", "wkl"},
		group:   "demand",
		short:   "What you asked to run, across every cluster it may use",
		long: `A workload is a recipe, a kind, a priority and a total scale — and no cluster.

Placement expands it into one deployment per cluster, and each of those carries
that cluster's share of the total. "Run gpt-20b on asia and europe" is one
workload, two deployments, one replica each.

kind is what makes it splittable: service and task fan out across clusters, a
gang is atomic and always has exactly one deployment, because ranks that are
co-scheduled cannot straddle clusters. The scale numbers are counted in
replicas for a service, ranks for a gang, completions for a task.`,
		example: `  hgr workload ls
  hgr workload ls --kind gang --phase pending
  hgr workload get chat
  hgr workload create chat --recipe llama-70b --project core --scale 8
  hgr workload scale chat 12`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Workload], error) {
			return b.Workloads(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.Workload, error) {
			return b.Workload(ctx, ref)
		},
		keys:  func(wl api.Workload) []string { return []string{wl.Name, wl.ID} },
		empty: "nothing declared to run",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "kind"},
			{Title: "phase"},
			{Title: "project", Wide: true},
			{Title: "priority"},
			{Title: "clusters", Align: ui.AlignRight},
			{Title: "scale", Align: ui.AlignRight},
			{Title: "ready", Align: ui.AlignRight},
			{Title: "strategy"},
			{Title: "paused"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, wl api.Workload) []string {
			return []string{
				wl.Name, wl.ID, wl.Kind, phaseColor(s, wl.Phase), wl.ProjectID, wl.PriorityClass,
				strconv.Itoa(len(wl.Deployments)),
				scaleCell(s, wl.Scale),
				strconv.Itoa(wl.Scale.Ready),
				wl.Strategy, warnIf(s, wl.Paused), agoAt(wl.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.project, "project", "", "limit to one project")
			cmd.Flags().StringVar(&f.kind, "kind", "", "filter by kind: service|task|gang")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by phase: pending|admitted|placed|running")
			cmd.Flags().StringVar(&f.recipe, "recipe", "", "limit to one recipe")
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "workloads with a share on one cluster")
			f.bindLabels(cmd)
		},
		groupKey: func(by string, wl api.Workload) (string, string, error) {
			switch by {
			case "kind":
				return wl.Kind, "kind", nil
			case "phase":
				return wl.Phase, "phase", nil
			case "project":
				return wl.ProjectID, "project", nil
			}
			return "", "", errs.New(errs.CodeUsage, "cannot group workloads by %q", by).
				WithCause("valid groupings: kind, phase, project")
		},
		detail: func(app *App, w io.Writer, s ui.Style, wl api.Workload) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(wl.Name), s.Dim(wl.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("kind", wl.Kind)
			kv.Add("phase", phaseColor(s, wl.Phase))
			kv.Add("recipe", wl.RecipeID)
			kv.Add("project", wl.ProjectID)
			kv.Add("priority class", wl.PriorityClass)
			kv.Add("preemptible", boolWord(wl.Preemptible))
			kv.Add("paused", warnIf(s, wl.Paused))
			if wl.Kind == "gang" {
				kv.Addf("gang size", "%d", wl.GangSize)
			}
			kv.Add("adapters", orDash(strings.Join(wl.AdapterArtifactIDs, " ")))
			kv.Add("overrides", labelString(wl.Overrides))
			kv.Add("labels", labelString(wl.Labels))
			kv.Add("single topology domain", orDash(boolPtrWord(wl.RequiresSingleTopologyDomain)))
			kv.Add("domain kind", orDash(str(wl.TopologyDomainKind)))
			kv.Add("deadline", ago(wl.DeadlineAt))
			kv.Add("revision", wl.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("scale"))
			skv := app.KV()
			skv.Addf("unit", "%s", wl.Scale.Unit)
			skv.Addf("desired", "%d", wl.Scale.Desired)
			skv.Addf("placed", "%d", wl.Scale.Placed)
			skv.Addf("ready", "%d", wl.Scale.Ready)
			skv.Addf("bounds", "%d-%d", wl.Scale.Min, wl.Scale.Max)
			skv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("placement"))
			pkv := app.KV()
			pkv.Add("strategy", wl.Strategy)
			pkv.Add("cluster floors", labelString(wl.ClusterFloors))
			pkv.Add("max clusters", num(wl.MaxClusters))
			pkv.Add("pinned cluster", orDash(str(wl.PinnedClusterID)))
			pkv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("deployments"))
			if len(wl.Deployments) == 0 {
				fmt.Fprintf(w, "  %s\n", s.Dim("no cluster is holding a share of this yet"))
				return
			}
			t := app.Table(
				ui.Column{Title: "cluster"},
				ui.Column{Title: "region"},
				ui.Column{Title: "deployment"},
				ui.Column{Title: "share", Align: ui.AlignRight},
				ui.Column{Title: "replicas", Align: ui.AlignRight},
				ui.Column{Title: "admission"},
				ui.Column{Title: "reason", Wide: true},
				ui.Column{Title: "authority"},
				ui.Column{Title: "enabled"},
			)
			for _, d := range wl.Deployments {
				t.Row(d.ClusterName, orDash(str(d.Region)), d.ID,
					strconv.Itoa(d.DesiredReplicas),
					fmt.Sprintf("%d/%d", d.Replicas.Ready, d.Replicas.Total),
					phaseColor(s, d.AdmissionPhase), orDash(str(d.AdmissionReason)),
					authorityWord(s, d.Authority), boolWord(d.Enabled))
			}
			t.Render(w)
		},
		next: func(wl api.Workload) []errs.Action {
			out := []errs.Action{
				{Description: "Its shares, per cluster", Command: "hgr deployment ls --workload " + wl.Name},
			}
			if wl.Paused {
				return append(out, errs.Action{Description: "Let it run again", Command: "hgr workload resume " + wl.Name})
			}
			return append(out,
				errs.Action{Description: "Change the total", Command: "hgr workload scale " + wl.Name + " <n>"},
				errs.Action{Description: "Take it out of one cluster", Command: "hgr workload drain " + wl.Name + " --cluster <cluster>"},
			)
		},
	}
}

// scaleCell shows what is placed against what was asked for, because the gap
// between them is the whole question a partial placement raises.
func scaleCell(s ui.Style, sc api.WorkloadScale) string {
	cell := fmt.Sprintf("%d/%d", sc.Placed, sc.Desired)
	if sc.Placed < sc.Desired {
		return s.Amber(cell)
	}
	return cell
}

func boolPtrWord(v *bool) string {
	if v == nil {
		return ""
	}
	return boolWord(*v)
}

func newWorkloadCreateCmd(app *App) *cobra.Command {
	var (
		recipe        string
		project       string
		cluster       string
		priorityClass string
		kind          string
		scale         int
		minScale      int
		maxScale      int
		strategy      string
		maxClusters   int
		floors        []string
		gangSize      int
		preemptible   bool
		adapters      []string
		labels        []string
		singleFabric  bool
		fabricKind    string
	)
	cmd := &cobra.Command{
		Use:     "create <name>",
		Aliases: []string{"new", "run"},
		Short:   "Declare something to run, without saying where",
		Long: `Create a workload. The cluster is not an argument.

--scale is the TOTAL across every cluster this workload may use — replicas for a
service, ranks for a gang, completions for a task — and placement divides it.
--strategy pack fills one cluster before reaching for another, which is the
default because it keeps a workload that fits in one cluster in one cluster;
--strategy spread levels it across everything it is allowed.

--cluster is the escape hatch, not the normal path: it pins the workload to one
cluster, is never checked for capacity, and forces a single deployment. A gang is
already single-cluster by construction, because its ranks are co-scheduled.`,
		Example: `  hgr workload create chat --recipe llama-70b --project core --scale 8
  hgr workload create chat --recipe llama-70b --project core --scale 8 --strategy spread
  hgr workload create sweep --recipe llama-70b --project core --kind task --scale 200
  hgr workload create train --recipe llama-70b --project core --kind gang --gang-size 64`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if recipe == "" || project == "" {
				return errs.New(errs.CodeInputRequired, "a workload needs --recipe and --project").
					WithCause("what to run, and who is charged for it")
			}
			parsedFloors, err := api.ParseKV(floors, "floor")
			if err != nil {
				return err
			}
			clusterFloors := map[string]int{}
			for k, v := range parsedFloors {
				n, err := strconv.Atoi(v)
				if err != nil {
					return errs.New(errs.CodeUsage, "--floor %s=%s is not a whole number of replicas", k, v)
				}
				clusterFloors[k] = n
			}
			parsedLabels, err := api.ParseKV(labels, "label")
			if err != nil {
				return err
			}
			labelMap := map[string]any{}
			for k, v := range parsedLabels {
				labelMap[k] = v
			}

			if kind == "gang" && !cmd.Flags().Changed("scale") {
				scale = gangSize
			}
			if !cmd.Flags().Changed("min-scale") {
				minScale = scale
			}
			if !cmd.Flags().Changed("max-scale") {
				maxScale = scale
			}

			req := api.WorkloadCreateRequest{
				Recipe: recipe, Name: args[0], Project: project, Cluster: cluster,
				PriorityClass: priorityClass, Kind: kind,
				MinScale: minScale, MaxScale: maxScale, DesiredScale: scale,
				Strategy: strategy, ClusterFloors: clusterFloors,
				Enabled: true, Preemptible: preemptible, GangSize: gangSize,
				AdapterArtifacts: adapters, Labels: labelMap,
			}
			if cmd.Flags().Changed("max-clusters") {
				req.MaxClusters = &maxClusters
			}
			// Sent only when it was typed. The field is a nullable override of the recipe's
			// own requirement, and null means the recipe's answer stands rather than that
			// there is no requirement, so a default written in unasked would freeze the
			// shape's derived answer onto a workload that should follow its recipe.
			if cmd.Flags().Changed("single-fabric") {
				req.RequiresSingleTopologyDomain = &singleFabric
			}
			if cmd.Flags().Changed("fabric-kind") {
				req.TopologyDomainKind = fabricKind
			}

			ctx, cancel := app.RequestContext()
			defer cancel()
			wl, err := app.Backend.CreateWorkload(ctx, req)
			if err != nil {
				return err
			}
			return emitWorkload(app, cmd, wl)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&recipe, "recipe", "", "the recipe to run")
	fl.StringVar(&project, "project", "", "the project that owns it")
	fl.StringVar(&cluster, "cluster", "", "pin to one cluster, overruling placement")
	fl.StringVar(&priorityClass, "priority-class", "", "priority class, defaulting to the project's")
	fl.StringVar(&kind, "kind", "service", "service|task|gang")
	fl.IntVar(&scale, "scale", 1, "total across every cluster it may use")
	fl.IntVar(&minScale, "min-scale", 1, "lower bound on the total (default: --scale)")
	fl.IntVar(&maxScale, "max-scale", 1, "upper bound on the total (default: --scale)")
	fl.StringVar(&strategy, "strategy", "pack", "pack|spread")
	fl.IntVar(&maxClusters, "max-clusters", 0, "cap how many clusters may hold a share")
	fl.StringArrayVar(&floors, "floor", nil, "per-cluster minimum share (CLUSTER=N, repeatable)")
	fl.IntVar(&gangSize, "gang-size", 1, "ranks that must be co-scheduled, for --kind gang")
	fl.BoolVar(&preemptible, "preemptible", false, "may be evicted for something of higher priority")
	fl.StringArrayVar(&adapters, "adapter", nil, "adapter artifact to load (repeatable)")
	fl.StringArrayVarP(&labels, "label", "l", nil, "label (KEY=VALUE, repeatable)")
	fl.BoolVar(&singleFabric, "single-fabric", false,
		"every device of a replica in one topology domain, overriding the recipe")
	fl.StringVar(&fabricKind, "fabric-kind", "", "which kind of domain --single-fabric means (nvlink, pcie_root, numa)")
	return cmd
}

func newWorkloadDeleteCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "rm <workload>",
		Aliases: []string{"delete", "destroy"},
		Short:   "Stop running a workload everywhere",
		Long: `Delete a workload and every cluster's share of it.

This is the only delete in this part of the model. A deployment cannot be
deleted on its own: taking a workload out of one cluster is ` + "`hgr workload drain`" + `,
which drives that cluster's share to zero and removes the row once its replicas
are gone.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			wl, err := workloadNoun().resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			if err := app.Confirm(fmt.Sprintf("delete %s and its %d deployment(s)?", wl.Name, len(wl.Deployments))); err != nil {
				return err
			}
			if err := app.Backend.DeleteWorkload(ctx, wl.ID); err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Names:   []string{wl.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s deleted\n", s.Bold(wl.Name))
				},
			})
		},
	}
}

// newWorkloadScaleCmd changes the total, never a cluster's share.
//
// The share is placement's answer and asserting one per cluster would be a
// second answer to a question that already has one. --floor is how a caller says
// something about where, and it is still a floor rather than a share.
func newWorkloadScaleCmd(app *App) *cobra.Command {
	var (
		wait     bool
		interval string
		reason   string
	)
	cmd := &cobra.Command{
		Use:   "scale <workload> <n>",
		Short: "Change a workload's total scale",
		Long: `Scale a workload to a new total across every cluster it may use.

An operation rather than a field write, because it is long-running and can fail:
the new total has to find quota and capacity, and it can sit pending on quota
indefinitely. --wait polls it to a terminal phase.`,
		Example: `  hgr workload scale chat 12
  hgr workload scale chat 12 --wait`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			desired, err := strconv.Atoi(args[1])
			if err != nil || desired < 0 {
				return errs.New(errs.CodeUsage, "scale takes a whole number of units, not %q", args[1])
			}
			return app.runWorkloadOperation(cmd, "workload.scale", args[0],
				map[string]any{"desired": desired}, reason, wait, interval)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&reason, "reason", "", "why — it outlives the person who ran it")
	fl.BoolVar(&wait, "wait", false, "poll until the operation reaches a terminal phase")
	fl.StringVar(&interval, "interval", "2s", "how often to poll while waiting")
	return cmd
}

// newWorkloadPauseCmd builds pause and resume, which differ only in the value
// they set. Paused is declared state that placement reads — the deployments stay
// where they are with a share of zero, so resuming does not re-place anything.
func newWorkloadPauseCmd(app *App, paused bool) *cobra.Command {
	var (
		wait     bool
		interval string
		reason   string
	)
	use, short := "pause <workload>", "Drive a workload's replicas to zero, keeping its placement"
	long := `Pause a workload.

Every cluster's share goes to zero and the deployment rows stay exactly where
they are, so resuming restores the shares rather than replacing the workload
somewhere else. Nothing is deleted and no endpoint target is removed.`
	if !paused {
		use, short = "resume <workload>", "Let a paused workload run again"
		long = `Resume a paused workload.

The shares come back from the totals, in the clusters that were already holding
them. A workload that was paused for long enough that its clusters filled up can
come back partially placed, which is what the placed count on ` + "`hgr workload get`" + `
is for.`
	}
	cmd := &cobra.Command{
		Use:   use,
		Short: short,
		Long:  long,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return app.runWorkloadOperation(cmd, "workload.pause", args[0],
				map[string]any{"paused": paused}, reason, wait, interval)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&reason, "reason", "", "why — it outlives the person who ran it")
	fl.BoolVar(&wait, "wait", false, "poll until the operation reaches a terminal phase")
	fl.StringVar(&interval, "interval", "2s", "how often to poll while waiting")
	return cmd
}

func newWorkloadDrainCmd(app *App) *cobra.Command {
	var (
		cluster  string
		wait     bool
		interval string
		reason   string
	)
	cmd := &cobra.Command{
		Use:   "drain <workload> --cluster <cluster>",
		Short: "Take a workload out of one cluster",
		Long: `Remove one cluster from a workload.

That cluster's share goes to zero, its endpoint targets are removed, and once
its replicas are gone the deployment row is deleted and the remaining clusters
are re-expanded to absorb the total. This is the reason a deployment has no
delete of its own.`,
		Example: `  hgr workload drain chat --cluster sjc-1
  hgr workload drain chat --cluster sjc-1 --wait`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if cluster == "" {
				return errs.New(errs.CodeInputRequired, "drain needs --cluster").
					WithCause("draining a workload everywhere is `hgr workload rm`")
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			c, err := clusterNoun().resolve(ctx, app, cluster)
			if err != nil {
				return err
			}
			return app.runWorkloadOperation(cmd, "workload.drain", args[0],
				map[string]any{"cluster": c.ID}, reason, wait, interval)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&cluster, "cluster", "", "the cluster to remove from the workload")
	fl.StringVar(&reason, "reason", "", "why — it outlives the person who ran it")
	fl.BoolVar(&wait, "wait", false, "poll until the operation reaches a terminal phase")
	fl.StringVar(&interval, "interval", "2s", "how often to poll while waiting")
	return cmd
}

// runWorkloadOperation is the one path scale, pause and drain share.
//
// The workload is resolved here so that a prefix or a name works the way it does
// on `get`, and so the operation carries the id rather than a name the plane
// would have to disambiguate a second time. No cluster is sent: these three are
// plane-local and are never dispatched to one.
func (a *App) runWorkloadOperation(cmd *cobra.Command, opType, ref string, params map[string]any, reason string, wait bool, interval string) error {
	ctx, cancel := a.RequestContext()
	defer cancel()

	wl, err := workloadNoun().resolve(ctx, a, ref)
	if err != nil {
		return err
	}
	if err := a.Confirm(fmt.Sprintf("start %s on %s?", opType, wl.Name)); err != nil {
		return err
	}
	op, err := a.Backend.CreateOperation(ctx, api.OperationRequest{
		Type: opType, Target: qualifyOperationTarget(opType, wl.ID),
		Reason: reason, Parameters: params,
	}, "")
	if err != nil {
		return err
	}
	if !wait {
		return a.emitOperation(cmd, op)
	}
	every, err := pollInterval(interval)
	if err != nil {
		return err
	}
	return a.waitForOperation(cmd, op.ID, every)
}

func emitWorkload(app *App, cmd *cobra.Command, wl api.Workload) error {
	n := workloadNoun()
	return app.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    wl,
		Names:   []string{wl.Name},
		Human: func(w io.Writer, s ui.Style, width int) {
			n.detail(app, w, s, wl)
		},
		Next: n.nextFor(wl),
	})
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
