package cli

import (
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
	"github.com/hangar/cli/internal/output"
	"github.com/hangar/cli/internal/ui"
	"github.com/spf13/cobra"
)

func newNodeCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "node",
		Aliases: []string{"nodes"},
		Short:   "Cluster members and their conditions",
		Long: `A node is one machine the cluster's operator reports as a member.

Nodes are observed, never declared: they appear because a pool provisioned
them. Cordon state is a boolean shown beside status, not a status of its own —
draining is an Operation, and the cordon is the fact it leaves behind.

Capacity questions are answered per device, not per node; the device counts
here are a rollup of the real thing.`,
	}
	group(cmd, "supply")
	cmd.AddCommand(newNodeListCmd(app), newNodeGetCmd(app))
	return cmd
}

func newNodeListCmd(app *App) *cobra.Command {
	f := &supplyFlags{}
	cmd := &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List cluster members",
		Example: `  hgr node ls
  hgr node ls --cluster iad-1
  hgr node ls --pool h100-prod --status ready
  hgr node ls --health degraded -o wide
  hgr node ls --cordoned -o name`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNodeList(app, cmdPath(cmd), f)
		},
	}
	f.bindShared(cmd, "substring match on name, id, cluster, pool, or shape")
	f.bindCluster(cmd)
	f.bindClass(cmd)
	f.bindHealth(cmd)
	f.bindDomain(cmd)
	f.bindStatus(cmd, "filter by status: ready|not_ready|unknown")
	cmd.Flags().StringVar(&f.pool, "pool", "", "limit to one pool (name, id, or unique prefix)")
	cmd.Flags().BoolVar(&f.cordoned, "cordoned", false, "only cordoned nodes")
	return cmd
}

// nodeList is the machine contract of `hgr node ls`.
type nodeList struct {
	Items      []api.Node `json:"items"`
	Count      int        `json:"count"`
	NextCursor string     `json:"next_cursor,omitempty"`
}

func runNodeList(app *App, path string, f *supplyFlags) error {
	opts, err := f.opts()
	if err != nil {
		return err
	}
	ctx, cancel := app.RequestContext()
	defer cancel()

	step := app.Report.Step("listing nodes")
	nodes, err := app.Backend.Nodes(ctx, opts)
	if err != nil {
		step.Fail("failed")
		return err
	}
	step.Done("%d nodes", len(nodes))

	names := make([]string, 0, len(nodes))
	rows := make([][]string, 0, len(nodes))
	for _, n := range nodes {
		names = append(names, n.Name)
		rows = append(rows, nodeRow(n))
	}

	var next []errs.Action
	if len(nodes) > 0 {
		next = []errs.Action{
			{Description: "Inspect one", Command: "hgr node get " + nodes[0].Name},
			{Description: "Its devices", Command: "hgr device ls --node " + nodes[0].Name},
		}
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    nodeList{Items: nodes, Count: len(nodes)},
		Names:   names,
		Columns: []string{"name", "id", "cluster", "pool", "status", "cordoned", "class", "devices", "unhealthy", "domains", "seen"},
		Rows:    rows,
		Human:   nodeTable(app, nodes),
		Next:    next,
	})
}

func nodeRow(n api.Node) []string {
	t := n.DeviceTotals()
	return []string{
		n.Name, n.ID, n.Cluster, orDash(n.Pool), n.Status,
		boolWord(n.Cordoned),
		nodeClasses(n),
		fmt.Sprintf("%d/%d", t.Allocated, t.Total),
		strconv.Itoa(t.Total - t.Healthy),
		domainNames(n.Topology),
		lastSeen(n.LastSeenAt),
	}
}

func nodeTable(app *App, nodes []api.Node) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(nodes) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim("no nodes match"))
			return
		}
		t := app.Table(
			ui.Column{Title: "name"},
			ui.Column{Title: "id", Wide: true},
			ui.Column{Title: "cluster"},
			ui.Column{Title: "pool"},
			ui.Column{Title: "status"},
			ui.Column{Title: "cordoned"},
			ui.Column{Title: "class"},
			ui.Column{Title: "devices", Align: ui.AlignRight},
			ui.Column{Title: "unhealthy", Align: ui.AlignRight},
			ui.Column{Title: "domains", Wide: true},
			ui.Column{Title: "seen", Align: ui.AlignRight},
		)
		for _, n := range nodes {
			d := n.DeviceTotals()
			t.Row(
				n.Name, n.ID, n.Cluster, orDash(n.Pool),
				nodeStatusColor(s, n),
				cordonWord(s, n.Cordoned),
				nodeClasses(n),
				fmt.Sprintf("%d/%d", d.Allocated, d.Total),
				unhealthyCell(s, d.Total-d.Healthy),
				domainNames(n.Topology),
				lastSeen(n.LastSeenAt),
			)
		}
		t.Render(w)
	}
}

func newNodeGetCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "get <node>",
		Aliases: []string{"show", "describe"},
		Short:   "Show one node in detail",
		Long: `Resolve a node by name, id, or unique prefix and print its membership,
conditions, device rollup, and the topology domains it contains.`,
		Example: `  hgr node get iad-1-n01
  hgr node get iad-1-n01 -o json
  hgr node get iad-1-n01 -o jsonpath=.topology[*].kind`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNodeGet(app, cmdPath(cmd), args[0])
		},
	}
}

func runNodeGet(app *App, path, ref string) error {
	ctx, cancel := app.RequestContext()
	defer cancel()

	n, err := app.Backend.Node(ctx, ref)
	if err != nil {
		return err
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    n,
		Names:   []string{n.Name},
		Human: func(w io.Writer, s ui.Style, width int) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(n.Name), s.Dim(n.ID))
			kv := app.KV()
			kv.Add("cluster", n.Cluster)
			kv.Add("status", nodeStatusColor(s, n))
			kv.Add("cordoned", cordonWord(s, n.Cordoned))
			kv.Add("pool", orDash(n.Pool))
			kv.Add("shape", orDash(n.Shape))
			kv.Addf("cpu", "%d cores", n.CPUCores)
			kv.Addf("memory", "%d GB", n.MemoryGB)
			kv.Add("kernel", orDash(n.Kernel))
			kv.Add("driver", orDash(n.Driver))
			kv.Add("labels", labelString(n.Labels))
			kv.Addf("joined", "%s ago", api.Age(n.JoinedAt))
			kv.Add("last seen", lastSeen(n.LastSeenAt))
			kv.Render(w)

			if len(n.Conditions) > 0 {
				fmt.Fprintf(w, "\n%s\n", s.Dim("conditions"))
				t := app.Table(
					ui.Column{Title: "type"},
					ui.Column{Title: "status"},
					ui.Column{Title: "reason"},
				)
				for _, c := range n.Conditions {
					t.Row(c.Type, conditionColor(s, c.Status), orDash(c.Reason))
				}
				t.Render(w)
			}

			if len(n.Devices) > 0 {
				fmt.Fprintf(w, "\n%s\n", s.Dim("devices"))
				t := app.Table(
					ui.Column{Title: "class"},
					ui.Column{Title: "total", Align: ui.AlignRight},
					ui.Column{Title: "healthy", Align: ui.AlignRight},
					ui.Column{Title: "allocated", Align: ui.AlignRight},
					ui.Column{Title: "free", Align: ui.AlignRight},
				)
				for _, d := range n.Devices {
					t.Row(d.Class,
						strconv.Itoa(d.Total),
						strconv.Itoa(d.Healthy),
						strconv.Itoa(d.Allocated),
						strconv.Itoa(d.Healthy-d.Allocated))
				}
				t.Render(w)
			}

			if len(n.Topology) > 0 {
				fmt.Fprintf(w, "\n%s\n", s.Dim("topology domains"))
				t := app.Table(
					ui.Column{Title: "domain"},
					ui.Column{Title: "id", Wide: true},
					ui.Column{Title: "kind"},
					ui.Column{Title: "devices", Align: ui.AlignRight},
				)
				for _, d := range n.Topology {
					t.Row(d.Name, d.ID, d.Kind, strconv.Itoa(d.Devices))
				}
				t.Render(w)
			}
		},
		Next: []errs.Action{
			{Description: "Devices on this node", Command: "hgr device ls --node " + n.Name},
			{Description: "The pool that provisioned it", Command: "hgr pool get " + n.Pool},
		},
	})
}

func nodeClasses(n api.Node) string {
	if len(n.Devices) == 0 {
		return "-"
	}
	out := make([]string, 0, len(n.Devices))
	for _, d := range n.Devices {
		out = append(out, d.Class)
	}
	return strings.Join(out, ",")
}

func domainNames(domains []api.TopologyDomainRef) string {
	if len(domains) == 0 {
		return "-"
	}
	out := make([]string, 0, len(domains))
	for _, d := range domains {
		out = append(out, fmt.Sprintf("%s(%d)", d.Kind, d.Devices))
	}
	return strings.Join(out, ",")
}

func nodeStatusColor(s ui.Style, n api.Node) string {
	switch n.Status {
	case api.NodeReady:
		return s.Green(n.Status)
	case api.NodeNotReady:
		return s.Red(n.Status)
	default:
		return s.Amber(n.Status)
	}
}

func cordonWord(s ui.Style, cordoned bool) string {
	if cordoned {
		return s.Amber("yes")
	}
	return s.Dim("no")
}

func conditionColor(s ui.Style, status string) string {
	if strings.EqualFold(status, "true") {
		return s.Green(status)
	}
	return s.Amber(status)
}

func unhealthyCell(s ui.Style, n int) string {
	if n == 0 {
		return "0"
	}
	return s.Amber(strconv.Itoa(n))
}

func boolWord(v bool) string {
	if v {
		return "yes"
	}
	return "no"
}

func lastSeen(t *time.Time) string {
	if t == nil {
		return "never"
	}
	return api.Age(*t)
}
