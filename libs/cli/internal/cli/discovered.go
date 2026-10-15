package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
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
		newRemoveWorkloadCmd(app),
		newRestoreWorkloadCmd(app),
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
		short:    "Workloads found running that the plane did not create",
		long: `Workloads the operator found running that the plane did not create: pods
holding accelerators.

Tracking one adds it to the inventory with an owner. It does not change
anything in the cluster, so nothing restarts.`,
		example: `  hgr discovered ls
  hgr discovered ls --status new
  hgr discovered ls --cluster iad-1 -o wide
  hgr discovered track llama-70b-vllm --project core
  hgr discovered rm llama-70b-vllm
  hgr discovered promote llama-70b-vllm --artifact llama-3.1-70b-instruct --project core`,
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
			cmd.Flags().StringVar(&f.status, "status", "", "filter by decision: new|tracked|ignored|dismissed")
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
					{Description: "The workload it belongs to", Command: "hgr workload ls --cluster " + d.ClusterName},
				}
			case "dismissed":
				return []errs.Action{
					{Description: "Put it back on the list", Command: "hgr discovered restore " + d.Name},
				}
			case "tracked":
				return []errs.Action{
					{Description: "Let the plane manage it",
						Command: "hgr discovered promote " + d.Name + " --artifact <artifact> --project <project>"},
					{Description: "Stop showing it", Command: "hgr discovered ignore " + d.Name},
				}
			}
			return []errs.Action{
				{Description: "Track it — nothing is declared and nothing restarts",
					Command: "hgr discovered track " + d.Name + " --project <project>"},
				{Description: "Stop showing it", Command: "hgr discovered ignore " + d.Name},
				{Description: "Take it off the list for good", Command: "hgr discovered rm " + d.Name},
			}
		},
	}
}

// newDecisionCmd builds track and ignore, which differ only in the word they
// record. Neither sends anything to a cluster, so neither asks to confirm.
func newDecisionCmd(app *App, decision string) *cobra.Command {
	var note string
	short := "Add a discovered workload to the inventory"
	long := `Track a workload the operator found running.

This records a decision and declares nothing. No deployment is created, no
serving spec is resolved, and no desired state reaches the cluster — so the
workload keeps running exactly as it is, with its weights where they already
are.`
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
			req := api.DecisionRequest{Note: note}
			if decision == decisionTracked && app.projectSet {
				req.Project = app.Project
			}
			out, err := app.Backend.DecideWorkload(ctx, found.ID, decision, req)
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
	cmd.Flags().StringVar(&note, "note", "", "why — it outlives the person who ran it")
	return cmd
}

func statusColor(s ui.Style, status string) string {
	switch status {
	case "tracked":
		return s.Green(status)
	case "ignored", "dismissed":
		return s.Dim(status)
	default:
		return s.Amber(status)
	}
}

// newRemoveWorkloadCmd takes a sighting off the list.
//
// It deletes nothing. The sighting is observed state with one writer, and the cluster reports the
// workload on every observation because it is still running — so what is recorded is the decision,
// and the listing is where it is honoured. `restore` undoes it.
func newRemoveWorkloadCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "rm <workload>",
		Aliases: []string{"remove", "dismiss"},
		Short:   "Take a discovered workload off the list for good",
		Long: `Remove a workload the operator found running from the discovered list.

Nothing changes in the cluster: the workload keeps running and the plane
keeps seeing it. It only stops appearing in the list.

` + "`hgr discovered ls --status dismissed`" + ` shows what has been removed, and
` + "`hgr discovered restore`" + ` puts one back.`,
		Example: `  hgr discovered rm llama-70b-vllm`,
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()

			found, err := app.Backend.DiscoveredWorkload(ctx, args[0])
			if err != nil {
				return err
			}
			if err := app.Backend.RemoveWorkload(ctx, found.ID); err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    map[string]any{"id": found.ID, "name": found.Name, "removed": true},
				Names:   []string{found.ID},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s removed — still running, never touched\n", s.Bold(found.Name))
				},
				Next: []errs.Action{
					{Description: "Put it back", Command: "hgr discovered restore " + found.Name},
				},
			})
		},
	}
	return cmd
}

// newRestoreWorkloadCmd undoes a removal by forgetting it was made.
func newRestoreWorkloadCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "restore <workload>",
		Short:   "Put a removed discovered workload back on the list",
		Example: `  hgr discovered restore llama-70b-vllm`,
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()

			found, err := app.Backend.DiscoveredWorkload(ctx, args[0])
			if err != nil {
				return err
			}
			out, err := app.Backend.RestoreWorkload(ctx, found.ID)
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
}

// newPromoteCmd builds the one verb here that writes declared state.
//
// The digest is not a flag. The command reads the workload, sends back the digest it was just
// shown, and the plane refuses if the workload moved in between — the caller cannot be asked to
// type a value whose whole purpose is that nobody chose it.
func newPromoteCmd(app *App) *cobra.Command {
	var (
		artifact      string
		name          string
		priorityClass string
	)
	cmd := &cobra.Command{
		Use:   "promote <workload>",
		Short: "Let the plane manage a workload it found running",
		Long: `Promote a discovered workload into a workload the plane declares.

The result is a workload pinned to the cluster it runs in, with one
deployment, managed under ` + "`hgr workload`" + ` like any other. Promotion does not
restart it: the deployment is pinned to the digest of what is running now.

Changing its serving configuration or overrides afterwards rolls it.

The replica bounds are taken from what is running, so promotion never scales
it.`,
		Example: `  hgr discovered promote llama-70b-vllm --artifact llama-3.1-70b-instruct --project core
  hgr discovered promote qwen2-72b --artifact llama-3.1-70b-instruct --project core --name qwen-sjc`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if artifact == "" || !app.projectSet {
				return errs.New(errs.CodeInputRequired, "promote needs --artifact and --project").
					WithCause("the plane has to know what this workload is and who owns it; --project may also come from HANGAR_PROJECT or context.project")
			}
			project := app.Project
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
			workload, err := app.Backend.PromoteWorkload(ctx, found.ID, api.PromoteRequest{
				Artifact:      artifact,
				Project:       project,
				Fingerprint:   *found.SpecFingerprint,
				Name:          name,
				PriorityClass: priorityClass,
			})
			if err != nil {
				return err
			}
			return emitWorkload(app, cmd, workload)
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&artifact, "artifact", "", "the weights this workload is running")
	fl.StringVar(&name, "name", "", "workload name, defaulting to the discovered workload's own")
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
