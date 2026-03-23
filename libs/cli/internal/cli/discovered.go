package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
	"github.com/hangar/cli/internal/output"
	"github.com/hangar/cli/internal/ui"
	"github.com/spf13/cobra"
)

const (
	decisionTracked = "track"
	decisionIgnored = "ignore"
)

func newDiscoveredCmd(app *App) *cobra.Command {
	cmd := discoveredNoun().command(app)
	cmd.AddCommand(
		newDecisionCmd(app, decisionTracked),
		newDecisionCmd(app, decisionIgnored),
		newPromoteCmd(app),
	)
	return cmd
}

func discoveredNoun() noun[api.DiscoveredWorkload] {
	return noun[api.DiscoveredWorkload]{
		name:     "discovered",
		aliases:  []string{"discoveries", "found"},
		plurally: "discovered workloads",
		group:    "demand",
		short:    "Workloads already running that we never declared",
		long: `What the operator found when it arrived: pods holding accelerators that this
plane did not put there.

A discovery is a sighting, not a deployment. Tracking one adds it to the
inventory and assigns it an owner — it does not declare it, does not render a
recipe against it, and never reaches the cluster. Nothing restarts, and a model
whose weights are already loaded stays loaded.`,
		example: `  hgr discovered ls
  hgr discovered ls --status new
  hgr discovered ls --cluster iad-1 -o wide
  hgr discovered track llama-70b-vllm --project core
  hgr discovered promote llama-70b-vllm --recipe llama-70b-vllm --project core`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.DiscoveredWorkload], error) {
			return b.DiscoveredWorkloads(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.DiscoveredWorkload, error) {
			return b.DiscoveredWorkload(ctx, ref)
		},
		keys:   func(d api.DiscoveredWorkload) []string { return []string{d.Name, d.ID} },
		getArg: "<workload>",
		empty:  "nothing found that we did not declare",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "cluster"},
			{Title: "namespace", Wide: true},
			{Title: "kind", Wide: true},
			{Title: "model"},
			{Title: "replicas", Align: ui.AlignRight},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "status"},
			{Title: "digest", Wide: true},
			{Title: "seen", Align: ui.AlignRight},
		},
		row: func(s ui.Style, d api.DiscoveredWorkload) []string {
			return []string{
				d.Name, d.ID, d.ClusterName, d.Namespace, d.Kind, orDash(str(d.Model)),
				fmt.Sprintf("%d/%d", d.ReplicasReady, d.ReplicasDesired),
				strconv.Itoa(d.DevicesTotal),
				statusColor(s, d.Status),
				shortDigest(str(d.SpecFingerprint)),
				ago(d.FirstSeenAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.namespace, "namespace", "", "limit to one namespace")
			cmd.Flags().StringVar(&f.status, "status", "", "filter by decision: new|tracked|ignored")
			cmd.Flags().StringVar(&f.search, "search", "", "match against the name")
		},
		groupKey: func(by string, d api.DiscoveredWorkload) (string, string, error) {
			switch by {
			case "cluster":
				return d.ClusterName, "cluster", nil
			case "status":
				return d.Status, "status", nil
			case "namespace":
				return d.Namespace, "namespace", nil
			}
			return "", "", errs.New(errs.CodeUsage, "cannot group discovered workloads by %q", by).
				WithCause("valid groupings: cluster, status, namespace")
		},
		detail: func(app *App, w io.Writer, s ui.Style, d api.DiscoveredWorkload) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(d.Name), s.Dim(d.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			kv := app.KV()
			kv.Add("cluster", d.ClusterName)
			kv.Add("namespace", d.Namespace)
			kv.Add("kind", d.Kind)
			kv.Add("model", orDash(str(d.Model)))
			kv.Add("images", orDash(strings.Join(d.Images, " ")))
			kv.Addf("replicas", "%d ready of %d", d.ReplicasReady, d.ReplicasDesired)
			kv.Addf("devices", "%d total · %d per replica", d.DevicesTotal, d.DevicesPerReplica)
			kv.Addf("nodes", "%d", len(d.NodeIDs))
			kv.Add("labels", labelString(d.Labels))
			kv.Add("first seen", ago(d.FirstSeenAt))
			kv.Add("last seen", ago(d.ObservedAt))
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("decision"))
			dkv := app.KV()
			dkv.Add("status", statusColor(s, d.Status))
			dkv.Add("deployment", orDash(str(d.DeploymentID)))
			dkv.Add("digest", orDash(shortDigest(str(d.SpecFingerprint))))
			dkv.Add("project", orDash(str(d.ProjectID)))
			dkv.Add("note", orDash(str(d.Note)))
			dkv.Add("by", orDash(str(d.DecidedBy)))
			dkv.Add("when", ago(d.DecidedAt))
			dkv.Render(w)
		},
		next: func(d api.DiscoveredWorkload) []errs.Action {
			switch d.Status {
			case "promoted":
				return []errs.Action{
					{Description: "Manage it", Command: "hgr deployment get " + str(d.DeploymentID)},
				}
			case "tracked":
				return []errs.Action{
					{Description: "Let the plane manage it",
						Command: "hgr discovered promote " + d.Name + " --recipe <recipe> --project <project>"},
					{Description: "Stop showing it", Command: "hgr discovered ignore " + d.Name},
				}
			}
			return []errs.Action{
				{Description: "Track it — nothing is declared and nothing restarts",
					Command: "hgr discovered track " + d.Name + " --project <project>"},
				{Description: "Stop showing it", Command: "hgr discovered ignore " + d.Name},
			}
		},
	}
}

// newDecisionCmd builds track and ignore, which differ only in the word they
// record. Neither sends anything to a cluster, so neither asks to confirm.
func newDecisionCmd(app *App, decision string) *cobra.Command {
	var (
		project string
		note    string
	)
	short := "Add a discovered workload to the inventory"
	long := `Track a workload the operator found running.

This records a decision and declares nothing. No deployment is created, no
recipe is resolved, and no desired state reaches the cluster — so the workload
keeps running exactly as it is, with its weights where they already are.`
	if decision == decisionIgnored {
		short = "Stop listing a discovered workload"
		long = `Ignore a workload the operator found running.

The sighting stays — the cluster keeps reporting it and it is still there. This
only says we have looked at it and do not want it in the default listing;
` + "`hgr discovered ls --status ignored`" + ` still shows it.`
	}

	cmd := &cobra.Command{
		Use:   decision + " <workload>",
		Short: short,
		Long:  long,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()

			found, err := app.Backend.DiscoveredWorkload(ctx, args[0])
			if err != nil {
				return err
			}
			out, err := app.Backend.DecideWorkload(ctx, found.ID, decision, api.DecisionRequest{
				Project: project,
				Note:    note,
			})
			if err != nil {
				return err
			}
			n := discoveredNoun()
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    out,
				Names:   []string{out.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					n.detail(app, w, s, out)
				},
				Next: n.nextFor(out),
			})
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&note, "note", "", "why — it outlives the person who ran it")
	if decision == decisionTracked {
		fl.StringVar(&project, "project", "", "the project that owns it")
	}
	return cmd
}

func statusColor(s ui.Style, status string) string {
	switch status {
	case "tracked":
		return s.Green(status)
	case "ignored":
		return s.Dim(status)
	default:
		return s.Amber(status)
	}
}

// newPromoteCmd builds the one verb here that writes declared state.
//
// The digest is not a flag. The command reads the workload, sends back the digest it was just
// shown, and the plane refuses if the workload moved in between — the caller cannot be asked to
// type a value whose whole purpose is that nobody chose it.
func newPromoteCmd(app *App) *cobra.Command {
	var (
		recipe        string
		project       string
		name          string
		priorityClass string
	)
	cmd := &cobra.Command{
		Use:   "promote <workload>",
		Short: "Let the plane manage a workload it found running",
		Long: `Promote a discovered workload into a deployment.

The plane starts holding a deployment for it, so it appears under ` + "`hgr deployment`" + ` and
is scaled and patched like any other. Nothing restarts to make that true: the
deployment is pinned to the digest of what is running right now, and the cluster
refuses to reconcile it while the live workload digests to anything else.

Changing the recipe or the overrides afterwards will roll it, as it should — that
is a change you asked for. Promotion itself is not.

Scale is not an argument. The bounds are taken from what the workload is already
running, so cataloguing it cannot scale it as a side effect.`,
		Example: `  hgr discovered promote llama-70b-vllm --recipe llama-70b-vllm --project core
  hgr discovered promote qwen2-72b --recipe qwen2-72b --project core --name qwen-sjc`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if recipe == "" || project == "" {
				return errs.New(errs.CodeInputRequired, "promote needs --recipe and --project").
					WithCause("the plane has to know what this workload is and who owns it")
			}
			ctx, cancel := app.RequestContext()
			defer cancel()

			found, err := app.Backend.DiscoveredWorkload(ctx, args[0])
			if err != nil {
				return err
			}
			if found.SpecFingerprint == nil || *found.SpecFingerprint == "" {
				return errs.New(errs.CodeInvalidState,
					"%q has no digest, so an adoption cannot be pinned to it", found.Name).
					WithCause("the cluster's operator has not reported what this workload is running")
			}
			deployment, err := app.Backend.PromoteWorkload(ctx, found.ID, api.PromoteRequest{
				Recipe:        recipe,
				Project:       project,
				Fingerprint:   *found.SpecFingerprint,
				Name:          name,
				PriorityClass: priorityClass,
			})
			if err != nil {
				return err
			}
			n := deploymentNoun()
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    deployment,
				Names:   []string{deployment.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					n.detail(app, w, s, deployment)
				},
				Next: n.nextFor(deployment),
			})
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&recipe, "recipe", "", "the recipe this workload is an instance of")
	fl.StringVar(&project, "project", "", "the project that owns it")
	fl.StringVar(&name, "name", "", "deployment name, defaulting to the workload's own")
	fl.StringVar(&priorityClass, "priority-class", "", "priority class, defaulting to the project's")
	return cmd
}

// shortDigest renders a fingerprint at the length a human compares by eye.
func shortDigest(fingerprint string) string {
	trimmed := strings.TrimPrefix(fingerprint, "sha256:")
	if len(trimmed) > 12 {
		return trimmed[:12]
	}
	return trimmed
}
