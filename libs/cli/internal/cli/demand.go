package cli

import (
	"context"
	"encoding/json"
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
			return []errs.Action{{Description: "Workloads that serve it", Command: "hgr workload ls --artifact " + a.ID}}
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
		long: `A deployment is one workload on one cluster: the unit the operator reconciles.

You do not create one. Placement creates it when it expands a workload, one per
cluster the workload landed on, and the replica bounds on it are THIS CLUSTER'S
share of the workload total — never the total. Serving, adapters, overrides and
labels are workload facts and are read there; admission is here, because a
workload can be running in one region and queued on quota in another.`,
		example: `  hgr deployment ls
  hgr deployment ls --cluster iad-1 --phase placed
  hgr deployment ls --workload chat
  hgr deployment get chat`,
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
				{Description: "Benchmark it", Command: "hgr benchmark run " + d.Name},
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
		long: `A workload is an artifact, the way to serve it, a priority and a total
number of replicas. It does not name a cluster.

Placement expands it into one deployment per cluster, and each deployment
carries that cluster's share of the total. Serving one model in two regions is
one workload with two deployments.

Workloads today are long-running inference services. Batch tasks and gang
scheduled jobs are on the roadmap; the kind field is there for them, but
nothing runs them yet.`,
		example: `  hgr workload ls
  hgr workload ls --phase pending
  hgr workload get chat
  hgr workload create assistant --project core --artifact llama-3.1-70b-instruct --class h100-80 --context-length 8192
  hgr workload scale chat 4`,
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
			{Title: "serving"},
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
				footprintSummary(wl.Serving),
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
			cmd.Flags().StringVar(&f.artifact, "artifact", "", "limit to one artifact")
			cmd.Flags().StringVar(&f.class, "class", "", "limit to one device class")
			cmd.Flags().StringVar(&f.digest, "serving-digest", "", "workloads running one exact configuration")
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
			kv.Add("artifact", wl.Serving.ArtifactID)
			kv.Add("served model", orDash(wl.ServedModelName))
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
			kv.Add("deadline", ago(wl.DeadlineAt))
			kv.Add("revision", wl.Revision)
			kv.Render(w)

			writeServing(app, w, s, wl)

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
				{Description: "Everything running this exact configuration",
					Command: "hgr workload ls --serving-digest " + wl.ServingDigest},
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

// footprintSummary is a serving configuration on one line — `8 x h100-80 · tp8 ·
// 128k · fp8` — the one cell that says how a workload is served.
// It formats the same fields in the same order as the console's own
// footprintSummary, so the two surfaces name a footprint identically.
func footprintSummary(sv api.ServingSpec) string {
	parts := []string{
		fmt.Sprintf("%d x %s", sv.Shape.DeviceCount, sv.Shape.DeviceClass),
		fmt.Sprintf("tp%d", sv.Parallelism.Tensor),
	}
	if sv.Parallelism.Pipeline > 1 {
		parts = append(parts, fmt.Sprintf("pp%d", sv.Parallelism.Pipeline))
	}
	if sv.ContextLength > 0 {
		parts = append(parts, shortTokens(sv.ContextLength))
	}
	if q := str(sv.Quantization); q != "" {
		parts = append(parts, q)
	}
	return strings.Join(parts, " · ")
}

// shortTokens abbreviates a context length only when it divides exactly, so a
// number nobody rounded is never shown rounded.
func shortTokens(v int) string {
	if v >= 1024 && v%1024 == 0 {
		return strconv.Itoa(v/1024) + "k"
	}
	return strconv.Itoa(v)
}

// writeServing prints the serving configuration a workload carries, digest
// first: the digest is the identity, and two workloads sharing one are running
// the same process.
func writeServing(app *App, w io.Writer, s ui.Style, wl api.Workload) {
	sv := wl.Serving
	fmt.Fprintf(w, "\n%s\n", s.Dim("serving"))
	kv := app.KV()
	kv.Add("digest", orDash(wl.ServingDigest))
	kv.Add("footprint", footprintSummary(sv))
	// "vllm on dynamo 1.4.1" rather than "vllm 1.4.1", because the number is the runtime's
	// and reading it as the engine's is the mistake the field was renamed to stop.
	kv.Add("engine", strings.TrimSpace(sv.Engine))
	kv.Add("engine runtime", orDash(strings.TrimSpace(sv.EngineRuntimeVersion)))
	kv.Add("engine runtime image", orDash(str(sv.EngineRuntimeImage)))
	kv.Add("frontend image", orDash(str(sv.FrontendImage)))
	kv.Add("engine args", labelString(sv.EngineArgs))
	kv.Add("resolved engine args", labelString(sv.ResolvedEngineArgs))
	kv.Add("imported args", labelString(sv.ImportedArgs))
	kv.Add("quantization", orDash(str(sv.Quantization)))
	kv.Add("dtype", orDash(str(sv.Dtype)))
	kv.Addf("parallelism", "tensor %d · pipeline %d · expert %d",
		sv.Parallelism.Tensor, sv.Parallelism.Pipeline, sv.Parallelism.Expert)
	kv.Add("topology", topologySummary(sv.Topology))
	kv.Addf("context", "%d", sv.ContextLength)
	kv.Add("max batch tokens", num(sv.MaxBatchTokens))
	kv.Render(w)

	fmt.Fprintf(w, "\n%s\n", s.Dim("shape, cache and resources"))
	ckv := app.KV()
	ckv.Add("device class", sv.Shape.DeviceClass)
	ckv.Addf("device count", "%d", sv.Shape.DeviceCount)
	ckv.Add("min device memory", mib(sv.Shape.DeviceMemoryMiBMin))
	ckv.Add("single topology domain", yesNo(s, sv.Shape.RequiresSingleTopologyDomain))
	ckv.Add("domain kind", orDash(str(sv.Shape.TopologyDomainKind)))
	ckv.Add("kv cache dtype", orDash(str(sv.Cache.KVCacheDtype)))
	ckv.Add("kv cache fraction", fraction(sv.Cache.KVCacheFraction))
	ckv.Add("prefix cache", boolWord(sv.Cache.PrefixCacheEnabled))
	ckv.Add("cpu", num(sv.Resources.CPUCores))
	ckv.Add("memory", mibPtr(sv.Resources.MemoryMiB))
	ckv.Add("disk", gib(sv.Resources.DiskGiB))
	ckv.Render(w)
}

func newWorkloadCreateCmd(app *App) *cobra.Command {
	var (
		artifact      string
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

		engine               string
		engineRuntimeVersion string
		engineRuntimeImage   string
		frontendImage        string
		engineArgs           []string
		quantization         string
		dtype                string
		tensor               int
		pipeline             int
		expert               int
		contextLength        int
		maxBatchTokens       int
		kvCacheDtype         string
		kvCacheFraction      float64
		prefixCache          bool
		class                string
		devices              int
		deviceMemoryMin      int
		cpuCores             int
		memoryMiB            int
		diskGiB              int
		outputModality       string
		modelClass           string
		samplingDefaults     []string
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
already single-cluster by construction, because its ranks are co-scheduled.

The serving flags are the workload's own: there is no shared configuration to
name and nothing to reuse by id. Two workloads given the same flags get the same
serving_digest, which is the whole of what sharing used to buy.

An image or video model runs on --engine omni (Dynamo's vLLM-Omni worker) and
needs --output-modality image|video instead of --context-length. --model-class
and --sampling-default go into the worker's stage configuration; the second is
how a distilled model gets its step count, because omni defaults every model to
50 inference steps.`,
		Example: `  hgr workload create assistant --project core --artifact llama-3.1-70b-instruct \
    --class h100-80 --context-length 8192 --scale 2
  hgr workload create assistant-tp8 --project core --artifact llama-3.1-70b-instruct \
    --class h100-80 --devices 8 --tensor-parallel 8 --context-length 32768
  hgr workload create z-image-turbo --project core --artifact llama-3.1-70b-instruct \
    --class h100-80 --engine omni --engine-runtime-version 1.4.1 \
    --output-modality image --sampling-default num_inference_steps=8`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if artifact == "" || !app.projectSet {
				return errs.New(errs.CodeInputRequired, "a workload needs --artifact and --project").
					WithCause("what to run, and who is charged for it; --project may also come from HANGAR_PROJECT or context.project")
			}
			project := app.Project
			omni := isOmniEngine(engine)
			if class == "" || (!omni && contextLength <= 0) {
				return errs.New(errs.CodeInputRequired, "a workload needs --class and --context-length").
					WithCause("a device count without a device class is not a shape, and the engine is told a context length either way")
			}
			if omni && outputModality == "" {
				return errs.New(errs.CodeInputRequired, "an omni workload needs --output-modality image or video").
					WithCause("it is the --output-modalities the worker will not start without")
			}
			if !omni && (outputModality != "" || modelClass != "" || len(samplingDefaults) > 0) {
				return errs.New(errs.CodeUsage, "--output-modality, --model-class and --sampling-default are for --engine omni")
			}
			behaviour, err := omniBehaviour(outputModality, modelClass, samplingDefaults)
			if err != nil {
				return err
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

			parsedEngineArgs, err := api.ParseKV(engineArgs, "engine-arg")
			if err != nil {
				return err
			}
			engineArgMap := map[string]any{}
			for k, v := range parsedEngineArgs {
				engineArgMap[k] = v
			}

			serving := api.ServingRequest{
				Artifact: artifact, Engine: engine, EngineRuntimeVersion: engineRuntimeVersion,
				ContextLength: contextLength,
				Shape: api.ServingShape{
					DeviceClass: class, DeviceCount: devices, DeviceMemoryMiBMin: deviceMemoryMin,
					RequiresSingleTopologyDomain: singleFabric,
					TopologyDomainKind:           optionalString(fabricKind),
				},
				EngineRuntimeImage: optionalString(engineRuntimeImage), FrontendImage: optionalString(frontendImage),
				EngineArgs:   engineArgMap,
				Quantization: optionalString(quantization), Dtype: optionalString(dtype),
				Parallelism: api.ServingParallelism{Tensor: tensor, Pipeline: pipeline, Expert: expert},
				Cache: api.ServingCache{
					KVCacheDtype:       optionalString(kvCacheDtype),
					PrefixCacheEnabled: prefixCache,
					PrefixCacheConfig:  map[string]any{},
				},
				Overlay:   api.ServingOverlay{PodSpec: map[string]any{}, Dynamo: map[string]any{}},
				Behaviour: behaviour,
			}
			if cmd.Flags().Changed("max-batch-tokens") {
				serving.MaxBatchTokens = &maxBatchTokens
			}
			if cmd.Flags().Changed("kv-cache-fraction") {
				serving.Cache.KVCacheFraction = &kvCacheFraction
			}
			if cmd.Flags().Changed("cpu") {
				serving.Resources.CPUCores = &cpuCores
			}
			if cmd.Flags().Changed("memory") {
				serving.Resources.MemoryMiB = &memoryMiB
			}
			if cmd.Flags().Changed("disk") {
				serving.Resources.DiskGiB = &diskGiB
			}

			req := api.WorkloadCreateRequest{
				Serving: serving,
				Name:    args[0], Project: project, Cluster: cluster,
				PriorityClass: priorityClass, Kind: kind,
				MinScale: minScale, MaxScale: maxScale, DesiredScale: scale,
				Strategy: strategy, ClusterFloors: clusterFloors,
				Enabled: true, Preemptible: preemptible, GangSize: gangSize,
				AdapterArtifacts: adapters, Labels: labelMap,
			}
			if cmd.Flags().Changed("max-clusters") {
				req.MaxClusters = &maxClusters
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
	fl.StringVar(&artifact, "artifact", "", "the weights to run")
	fl.StringVar(&cluster, "cluster", "", "pin to one cluster, overruling placement")
	fl.StringVar(&priorityClass, "priority-class", "", "priority class, defaulting to the project's")
	fl.StringVar(&kind, "kind", "service", "service; task and gang are on the roadmap and nothing runs them yet")
	fl.IntVar(&scale, "scale", 1, "total across every cluster it may use")
	fl.IntVar(&minScale, "min-scale", 1, "lower bound on the total (default: --scale)")
	fl.IntVar(&maxScale, "max-scale", 1, "upper bound on the total (default: --scale)")
	fl.StringVar(&strategy, "strategy", "pack", "pack|spread")
	fl.IntVar(&maxClusters, "max-clusters", 0, "cap how many clusters may hold a share")
	fl.StringArrayVar(&floors, "floor", nil, "per-cluster minimum share (CLUSTER=N, repeatable)")
	fl.IntVar(&gangSize, "gang-size", 1, "ranks that must be co-scheduled, for --kind gang (roadmap)")
	fl.BoolVar(&preemptible, "preemptible", false, "may be evicted for something of higher priority")
	fl.StringArrayVar(&adapters, "adapter", nil, "adapter artifact to load (repeatable)")
	fl.StringArrayVarP(&labels, "label", "l", nil, "label (KEY=VALUE, repeatable)")
	fl.BoolVar(&singleFabric, "single-fabric", false,
		"every device of a replica in one topology domain")
	fl.StringVar(&fabricKind, "fabric-kind", "", "which kind of domain --single-fabric means (nvlink, pcie_root, numa)")

	fl.StringVar(&engine, "engine", "vllm", "inference engine")
	fl.StringVar(&engineRuntimeVersion, "engine-runtime-version", "",
		"Dynamo runtime tag the engine runs in, e.g. 1.4.1 — not the engine's own version")
	fl.StringVar(&engineRuntimeImage, "engine-runtime-image", "",
		"override the whole address instead of deriving it from --engine and --engine-runtime-version")
	fl.StringVar(&frontendImage, "frontend-image", "", "override the image the frontend runs from")
	fl.StringArrayVar(&engineArgs, "engine-arg", nil,
		"engine flag, beating every derived one (KEY=VALUE, repeatable)")
	fl.StringVar(&quantization, "quantization", "", "weight quantization, e.g. fp8")
	fl.StringVar(&dtype, "dtype", "", "compute dtype, e.g. bfloat16")
	fl.IntVar(&tensor, "tensor-parallel", 1, "tensor-parallel degree")
	fl.IntVar(&pipeline, "pipeline-parallel", 1, "pipeline-parallel degree")
	fl.IntVar(&expert, "expert-parallel", 1, "expert-parallel degree")
	fl.IntVar(&contextLength, "context-length", 0, "context length in tokens")
	fl.IntVar(&maxBatchTokens, "max-batch-tokens", 0, "cap on tokens in one batch")
	fl.StringVar(&kvCacheDtype, "kv-cache-dtype", "", "KV cache dtype, e.g. fp8")
	fl.Float64Var(&kvCacheFraction, "kv-cache-fraction", 0, "share of device memory the KV cache may take")
	fl.BoolVar(&prefixCache, "prefix-cache", true, "reuse prefixes across requests")
	fl.StringVar(&class, "class", "", "device class every replica is placed on")
	fl.IntVar(&devices, "devices", 1, "devices per replica")
	fl.IntVar(&deviceMemoryMin, "device-memory-min", 0, "minimum device memory in MiB")
	fl.IntVar(&cpuCores, "cpu", 0, "CPU cores per replica")
	fl.IntVar(&memoryMiB, "memory", 0, "host memory per replica, in MiB")
	fl.IntVar(&diskGiB, "disk", 0, "scratch disk per replica, in GiB")
	fl.StringVar(&outputModality, "output-modality", "", "what an omni worker emits: image|video")
	fl.StringVar(&modelClass, "model-class", "", "omni pipeline class, e.g. LTX2Pipeline (default: inferred by omni)")
	fl.StringArrayVar(&samplingDefaults, "sampling-default", nil,
		"omni default sampling parameter (KEY=VALUE, repeatable), e.g. num_inference_steps=8")
	return cmd
}

// isOmniEngine is the plane's and the operator's set of spellings for vLLM-Omni.
func isOmniEngine(engine string) bool {
	switch strings.ToLower(strings.TrimSpace(engine)) {
	case "omni", "vllm-omni", "vllm_omni":
		return true
	}
	return false
}

// omniBehaviour is the serving block an omni workload sends, or nil for every
// other workload so that the plane's defaults stand untouched.
//
// A sampling value is sent as the JSON it reads as — 8 as a number, 4.0 as a
// number, true as a boolean — and as a string only when it is none of those,
// because the stage config hands it to the pipeline as it arrives.
func omniBehaviour(modality, modelClass string, defaults []string) (*api.ServingBehaviour, error) {
	if modality == "" {
		return nil, nil
	}
	if modality != "image" && modality != "video" {
		return nil, errs.New(errs.CodeUsage, "--output-modality is image or video, not %q", modality)
	}
	parsed, err := api.ParseKV(defaults, "sampling-default")
	if err != nil {
		return nil, err
	}
	b := api.DefaultServingBehaviour()
	b.OutputModality = &modality
	if modelClass == "" && len(parsed) == 0 {
		return &b, nil
	}
	d := &api.ServingDiffusion{ModelClassName: optionalString(modelClass)}
	if len(parsed) > 0 {
		d.DefaultSamplingParams = map[string]any{}
		for k, v := range parsed {
			var value any
			if err := json.Unmarshal([]byte(v), &value); err != nil {
				value = v
			}
			d.DefaultSamplingParams[k] = value
		}
	}
	b.Diffusion = d
	return &b, nil
}

// optionalString reads an unset string flag as absent rather than as empty. The
// fields it feeds are nullable on the wire and empty is not null.
func optionalString(v string) *string {
	if v == "" {
		return nil
	}
	return &v
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
			wl, err := workloadNoun().resolveExact(ctx, app, args[0])
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
				Data:    deleted(wl.ID, wl.Name),
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
			c, err := clusterNoun().resolveExact(ctx, app, cluster)
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

	wl, err := workloadNoun().resolveExact(ctx, a, ref)
	if err != nil {
		return err
	}
	if err := a.Confirm(fmt.Sprintf("start %s on %s?", opType, wl.Name)); err != nil {
		return err
	}
	op, err := a.Backend.CreateOperation(ctx, api.OperationRequest{
		Type: opType, Target: qualifyOperationTarget(opType, wl.ID),
		Reason: reason, Parameters: params,
	}, a.idempotencyKey(""))
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
		Example: `  hgr deployment events chat
  hgr deployment events chat --since 1h
  hgr deployment events chat --severity error`,
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
			kv.Add("slug", e.Slug)
			kv.Add("aliases", orDash(strings.Join(e.Aliases, ", ")))
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

			renderSurface(app, w, s, e.Surface)
		},
		next: func(e api.Endpoint) []errs.Action {
			if len(e.Targets) == 0 {
				return nil
			}
			return []errs.Action{{Description: "Its primary deployment", Command: "hgr deployment get " + e.Targets[0].DeploymentID}}
		},
		extra: func(app *App) []*cobra.Command {
			return []*cobra.Command{newEndpointSLACmd(app)}
		},
	}
}

// renderSurface prints the terms on which the endpoint's API is exposed.
//
// A field nobody declared is a dash and never a default: the surface is where an operator reads
// what was sold, and printing the gateway's own behaviour as if it had been declared is how a term
// nobody agreed to gets repeated to a customer.
func renderSurface(app *App, w io.Writer, s ui.Style, surface api.EndpointSurface) {
	fmt.Fprintf(w, "\n%s\n", s.Dim("surface"))
	kv := app.KV()
	kv.Add("protocols", orDash(strings.Join(surface.Protocols, " ")))
	kv.Add("auth", orDash(surface.AuthMode))
	kv.Add("errors", orDash(surface.ErrorShape))
	kv.Add("streaming", orDash(surface.StreamingTransport))
	kv.Add("cors", orDashBool(surface.CORSEnabled))
	kv.Add("max request bytes", orDashInt(surface.MaxRequestBytes))
	kv.Add("request timeout", orDashInt(surface.RequestTimeoutSecond))
	kv.Add("output length", orDash(strings.Join(surface.OutputLengthParams, " ")))
	kv.Render(w)

	if surface.Policy == nil || len(surface.Policy.Rules) == 0 {
		return
	}
	fmt.Fprintf(w, "\n%s\n", s.Dim(fmt.Sprintf("policy v%d", surface.Policy.Version)))
	t := app.Table(
		ui.Column{Title: "scope"},
		ui.Column{Title: "subject"},
		ui.Column{Title: "terms"},
	)
	for _, rule := range surface.Policy.Rules {
		t.Row(ruleScope(rule), ruleSubject(rule), orDash(strings.Join(ruleTerms(rule), " · ")))
	}
	t.Render(w)
}

// ruleScope is where a rule was measured. An empty scope is every route and both transport modes,
// which is what an unscoped rule has always meant, so it prints as "any" rather than as a blank.
func ruleScope(rule api.PolicyRule) string {
	parts := []string{"any route"}
	if rule.Route != "" {
		parts[0] = rule.Route
	}
	if rule.Stream != nil {
		if *rule.Stream {
			parts = append(parts, "streaming")
		} else {
			parts = append(parts, "non-streaming")
		}
	}
	if rule.WhenPresent != "" {
		parts = append(parts, "when "+rule.WhenPresent+" is sent")
	}
	return strings.Join(parts, " · ")
}

// ruleSubject names the body path a rule reads, or the gateway rule it invokes. The two are
// separate namespaces and a predicate is marked as one so neither reads as the other.
func ruleSubject(rule api.PolicyRule) string {
	if rule.Key != "" {
		return rule.Key
	}
	if rule.Predicate != "" {
		return "predicate:" + rule.Predicate
	}
	return "-"
}

// ruleTerms is what one rule says, refusals first and rewrites after.
func ruleTerms(rule api.PolicyRule) []string {
	var terms []string
	if rule.Deny {
		terms = append(terms, "denied")
	}
	if len(rule.Enum) > 0 {
		values := make([]string, 0, len(rule.Enum))
		for _, v := range rule.Enum {
			values = append(values, fmt.Sprintf("%v", v))
		}
		terms = append(terms, "one of "+strings.Join(values, ", "))
	}
	if rule.Min != nil {
		terms = append(terms, fmt.Sprintf(">= %v", *rule.Min))
	}
	if rule.Max != nil {
		terms = append(terms, fmt.Sprintf("<= %v", *rule.Max))
	}
	if rule.Default != nil {
		terms = append(terms, fmt.Sprintf("defaults to %v", rule.Default))
	}
	if rule.Drop {
		terms = append(terms, "dropped")
	}
	if rule.ValueFrom != "" {
		terms = append(terms, "copied from "+rule.ValueFrom)
	}
	return terms
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

// topologySummary is "aggregated" or the roles and their weights, e.g.
// "disaggregated · prefill 1 · decode 3".
func topologySummary(t api.ServingTopology) string {
	parts := []string{orDash(t.ServingTopology)}
	for _, r := range t.Roles {
		parts = append(parts, fmt.Sprintf("%s %d", r.Role, r.Weight))
	}
	return strings.Join(parts, " · ")
}
