package cli

import (
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
	"github.com/hangar/cli/internal/output"
	"github.com/hangar/cli/internal/ui"
	"github.com/spf13/cobra"
)

func newClusterCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "cluster",
		Aliases: []string{"clusters"},
		Short:   "Enrolled GPU clusters and the state they report",
		Long: `A cluster is one Kubernetes installation we own and operate.

The plane holds its enrolment identity, region, and capabilities; the
cluster-resident operator dials out and reports membership, device health, and
what is running. Both halves are shown together.`,
	}
	group(cmd, "supply")
	cmd.AddCommand(newClusterListCmd(app), newClusterGetCmd(app))
	return cmd
}

func newClusterListCmd(app *App) *cobra.Command {
	f := &listFlags{}
	cmd := &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List enrolled clusters",
		Example: `  hgr cluster ls
  hgr cluster ls --region us-east --status active
  hgr cluster ls -o json | jq '.items[].name'
  hgr cluster ls -o name`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClusterList(app, cmdPath(cmd), f)
		},
	}
	f.bind(cmd)
	return cmd
}

// clusterList is the machine contract of `hgr cluster ls`. It is an object,
// not a bare array, so pagination and counts can be added without breaking
// consumers that already parse it.
type clusterList struct {
	Items      []api.Cluster `json:"items"`
	Count      int           `json:"count"`
	NextCursor string        `json:"next_cursor,omitempty"`
}

func runClusterList(app *App, path string, f *listFlags) error {
	opts, err := f.opts()
	if err != nil {
		return err
	}
	ctx, cancel := app.RequestContext()
	defer cancel()

	step := app.Report.Step("listing clusters")
	clusters, err := app.Backend.Clusters(ctx, opts)
	if err != nil {
		step.Fail("failed")
		return err
	}
	step.Done("%d clusters", len(clusters))

	names := make([]string, 0, len(clusters))
	rows := make([][]string, 0, len(clusters))
	for _, c := range clusters {
		names = append(names, c.Name)
		t := c.DeviceTotals()
		rows = append(rows, []string{
			c.Name, c.ID, c.Region, c.Phase,
			strconv.Itoa(c.Nodes),
			fmt.Sprintf("%d/%d", t.Allocated, t.Total),
			strconv.Itoa(c.Deployments),
			deviceClasses(c),
			orDash(c.AgentVersion),
			syncAge(c),
		})
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    clusterList{Items: clusters, Count: len(clusters)},
		Names:   names,
		Columns: []string{"name", "id", "region", "phase", "nodes", "devices", "deployments", "classes", "agent", "synced"},
		Rows:    rows,
		Human:   clusterTable(app, clusters),
		Next:    listNext(clusters),
	})
}

func clusterTable(app *App, clusters []api.Cluster) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(clusters) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim("no clusters enrolled"))
			return
		}
		t := app.Table(
			ui.Column{Title: "name"},
			ui.Column{Title: "id", Wide: true},
			ui.Column{Title: "region"},
			ui.Column{Title: "phase"},
			ui.Column{Title: "nodes", Align: ui.AlignRight},
			ui.Column{Title: "devices", Align: ui.AlignRight},
			ui.Column{Title: "deploys", Align: ui.AlignRight},
			ui.Column{Title: "classes"},
			ui.Column{Title: "agent", Wide: true},
			ui.Column{Title: "synced", Align: ui.AlignRight},
		)
		for _, c := range clusters {
			d := c.DeviceTotals()
			t.Row(
				c.Name, c.ID, c.Region, phaseColor(s, c),
				strconv.Itoa(c.Nodes),
				fmt.Sprintf("%d/%d", d.Allocated, d.Total),
				strconv.Itoa(c.Deployments),
				deviceClasses(c),
				orDash(c.AgentVersion),
				syncAge(c),
			)
		}
		t.Render(w)
	}
}

func newClusterGetCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "get <cluster>",
		Aliases: []string{"show", "describe"},
		Short:   "Show one cluster in detail",
		Long: `Resolve a cluster by name, id, or unique prefix and print everything the
plane knows about it: declared enrolment on one side, the operator's last
reported state on the other.`,
		Example: `  hgr cluster get iad-1
  hgr cluster get clu_01hq0v8p2m -o json
  hgr cluster get iad-1 -o jsonpath=.devices[*].class`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClusterGet(app, cmdPath(cmd), args[0])
		},
	}
	return cmd
}

func runClusterGet(app *App, path, ref string) error {
	ctx, cancel := app.RequestContext()
	defer cancel()

	c, err := app.Backend.Cluster(ctx, ref)
	if err != nil {
		return err
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    c,
		Names:   []string{c.Name},
		Human: func(w io.Writer, s ui.Style, width int) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(c.Name), s.Dim(c.ID))
			kv := app.KV()
			kv.Add("region", c.Region)
			kv.Add("phase", phaseColor(s, c))
			kv.Add("reachable", yesNo(s, c.Reachable))
			kv.Add("agent", orDash(c.AgentVersion))
			kv.Add("capabilities", strings.Join(c.Capabilities, " "))
			kv.Addf("nodes", "%d", c.Nodes)
			kv.Addf("deployments", "%d", c.Deployments)
			kv.Add("labels", labelString(c.Labels))
			kv.Addf("enrolled", "%s ago", api.Age(c.EnrolledAt))
			kv.Add("last sync", syncAge(c))
			kv.Render(w)

			if len(c.Devices) == 0 {
				return
			}
			fmt.Fprintf(w, "\n%s\n", s.Dim("devices"))
			t := app.Table(
				ui.Column{Title: "class"},
				ui.Column{Title: "total", Align: ui.AlignRight},
				ui.Column{Title: "healthy", Align: ui.AlignRight},
				ui.Column{Title: "allocated", Align: ui.AlignRight},
				ui.Column{Title: "free", Align: ui.AlignRight},
			)
			for _, d := range c.Devices {
				t.Row(d.Class,
					strconv.Itoa(d.Total),
					strconv.Itoa(d.Healthy),
					strconv.Itoa(d.Allocated),
					strconv.Itoa(d.Healthy-d.Allocated))
			}
			t.Render(w)
		},
		Next: []errs.Action{
			{Description: "Devices on this cluster", Command: "hgr device ls --cluster " + c.Name},
			{Description: "What is running here", Command: "hgr deployment ls --cluster " + c.Name},
		},
	})
}

func listNext(clusters []api.Cluster) []errs.Action {
	if len(clusters) == 0 {
		return nil
	}
	return []errs.Action{{Description: "Inspect one", Command: "hgr cluster get " + clusters[0].Name}}
}

func phaseColor(s ui.Style, c api.Cluster) string {
	switch {
	case c.Phase == api.PhaseActive && !c.Reachable:
		return s.Amber("active (no sync)")
	case c.Phase == api.PhaseActive:
		return s.Green(c.Phase)
	case c.Phase == api.PhaseDraining, c.Phase == api.PhasePending:
		return s.Amber(c.Phase)
	case c.Phase == api.PhaseDisabled:
		return s.Dim(c.Phase)
	}
	return c.Phase
}

func deviceClasses(c api.Cluster) string {
	if len(c.Devices) == 0 {
		return "-"
	}
	out := make([]string, 0, len(c.Devices))
	for _, d := range c.Devices {
		out = append(out, d.Class)
	}
	return strings.Join(out, ",")
}

func syncAge(c api.Cluster) string {
	if c.LastSyncAt == nil {
		return "never"
	}
	return api.Age(*c.LastSyncAt)
}

func labelString(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]string, 0, len(keys))
	for _, k := range keys {
		out = append(out, k+"="+labels[k])
	}
	return strings.Join(out, " ")
}

func yesNo(s ui.Style, v bool) string {
	if v {
		return s.Green("yes")
	}
	return s.Amber("no")
}

func orDash(s string) string {
	if strings.TrimSpace(s) == "" {
		return "-"
	}
	return s
}
