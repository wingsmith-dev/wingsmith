package cli

import (
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

func newPoolCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "pool",
		Aliases: []string{"pools"},
		Short:   "Declared provisioning specs that carve homogeneous slices",
		Long: `A pool is declared, not observed: a device class, a node shape, size
bounds, and where the nodes come from.

It exists so placement has something homogeneous to bind against — you cannot
tensor-parallel across an A100 and an H100, so a mixed cluster is carved into
pools that are not mixed. Membership underneath it is observed, and the two
are shown apart.`,
	}
	group(cmd, "supply")
	cmd.AddCommand(newPoolListCmd(app), newPoolGetCmd(app))
	return cmd
}

func newPoolListCmd(app *App) *cobra.Command {
	f := &supplyFlags{}
	cmd := &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List provisioning pools",
		Example: `  hgr pool ls
  hgr pool ls --cluster sjc-1
  hgr pool ls --class h100-80 --status active
  hgr pool ls -o json | jq '.items[] | {name, min_nodes, max_nodes}'`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPoolList(app, cmdPath(cmd), f)
		},
	}
	f.bindShared(cmd, "substring match on name, id, cluster, class, or source")
	f.bindCluster(cmd)
	f.bindClass(cmd)
	f.bindDomain(cmd)
	f.bindStatus(cmd, "filter by status: active|pending|paused|disabled")
	return cmd
}

// poolList is the machine contract of `hgr pool ls`.
type poolList struct {
	Items      []api.Pool `json:"items"`
	Count      int        `json:"count"`
	NextCursor string     `json:"next_cursor,omitempty"`
}

func runPoolList(app *App, path string, f *supplyFlags) error {
	opts, err := f.opts()
	if err != nil {
		return err
	}
	ctx, cancel := app.RequestContext()
	defer cancel()

	step := app.Report.Step("listing pools")
	pools, err := app.Backend.Pools(ctx, opts)
	if err != nil {
		step.Fail("failed")
		return err
	}
	step.Done("%d pools", len(pools))

	names := make([]string, 0, len(pools))
	rows := make([][]string, 0, len(pools))
	for _, p := range pools {
		names = append(names, p.Name)
		rows = append(rows, []string{
			p.Name, p.ID, p.Cluster, p.Status, p.DeviceClass, p.Source,
			fmt.Sprintf("%d/%d", p.ReadyNodes, p.Nodes),
			boundsCell(p),
			fmt.Sprintf("%d/%d", p.Devices.Allocated, p.Devices.Total),
			strconv.Itoa(p.Devices.Total - p.Devices.Healthy),
			orDash(p.TopologyKind),
		})
	}

	var next []errs.Action
	if len(pools) > 0 {
		next = []errs.Action{
			{Description: "Inspect one", Command: "hgr pool get " + pools[0].Name},
			{Description: "Its nodes", Command: "hgr node ls --pool " + pools[0].Name},
		}
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    poolList{Items: pools, Count: len(pools)},
		Names:   names,
		Columns: []string{"name", "id", "cluster", "status", "class", "source", "nodes", "bounds", "devices", "unhealthy", "topology"},
		Rows:    rows,
		Human:   poolTable(app, pools),
		Next:    next,
	})
}

func poolTable(app *App, pools []api.Pool) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(pools) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim("no pools declared"))
			return
		}
		t := app.Table(
			ui.Column{Title: "name"},
			ui.Column{Title: "id", Wide: true},
			ui.Column{Title: "cluster"},
			ui.Column{Title: "status"},
			ui.Column{Title: "class"},
			ui.Column{Title: "source"},
			ui.Column{Title: "nodes", Align: ui.AlignRight},
			ui.Column{Title: "bounds", Align: ui.AlignRight},
			ui.Column{Title: "devices", Align: ui.AlignRight},
			ui.Column{Title: "unhealthy", Align: ui.AlignRight},
			ui.Column{Title: "topology"},
		)
		for _, p := range pools {
			t.Row(
				p.Name, p.ID, p.Cluster, poolStatusColor(s, p.Status), p.DeviceClass, p.Source,
				fmt.Sprintf("%d/%d", p.ReadyNodes, p.Nodes),
				boundsCell(p),
				fmt.Sprintf("%d/%d", p.Devices.Allocated, p.Devices.Total),
				unhealthyCell(s, p.Devices.Total-p.Devices.Healthy),
				orDash(p.TopologyKind),
			)
		}
		t.Render(w)
	}
}

func newPoolGetCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "get <pool>",
		Aliases: []string{"show", "describe"},
		Short:   "Show one pool in detail",
		Long: `Resolve a pool by name, id, or unique prefix and print the declared
provisioning spec and the membership observed against it, separately — the
first is what we asked for, the second is what the cluster reports.`,
		Example: `  hgr pool get h100-prod
  hgr pool get h100-prod -o json
  hgr pool get h100-prod -o jsonpath=.devices.allocated`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPoolGet(app, cmdPath(cmd), args[0])
		},
	}
}

func runPoolGet(app *App, path, ref string) error {
	ctx, cancel := app.RequestContext()
	defer cancel()

	p, err := app.Backend.Pool(ctx, ref)
	if err != nil {
		return err
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    p,
		Names:   []string{p.Name},
		Human: func(w io.Writer, s ui.Style, width int) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(p.Name), s.Dim(p.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("cluster", p.Cluster)
			kv.Add("device class", p.DeviceClass)
			kv.Add("node shape", orDash(p.Shape))
			kv.Addf("devices/node", "%d", p.DevicesPer)
			kv.Add("size", boundsCell(p))
			kv.Add("source", p.Source)
			kv.Add("topology", orDash(p.TopologyKind))
			kv.Add("labels", labelString(p.Labels))
			kv.Add("taints", orDash(strings.Join(p.Taints, " ")))
			kv.Addf("created", "%s ago", api.Age(p.CreatedAt))
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			okv := app.KV()
			okv.Add("status", poolStatusColor(s, p.Status))
			okv.Addf("nodes", "%d (%d ready)", p.Nodes, p.ReadyNodes)
			okv.Addf("devices", "%d", p.Devices.Total)
			okv.Addf("healthy", "%d", p.Devices.Healthy)
			okv.Addf("allocated", "%d", p.Devices.Allocated)
			okv.Addf("free", "%d", p.Devices.Healthy-p.Devices.Allocated)
			okv.Add("updated", lastSeen(p.UpdatedAt))
			okv.Render(w)
		},
		Next: []errs.Action{
			{Description: "Nodes in this pool", Command: "hgr node ls --pool " + p.Name},
			{Description: "Free devices in this pool", Command: "hgr device ls --pool " + p.Name + " --status free"},
		},
	})
}

func boundsCell(p api.Pool) string {
	return fmt.Sprintf("%d-%d", p.MinNodes, p.MaxNodes)
}

func poolStatusColor(s ui.Style, status string) string {
	switch status {
	case api.PoolActive:
		return s.Green(status)
	case api.PoolPending, api.PoolPaused:
		return s.Amber(status)
	case api.PoolDisabled:
		return s.Dim(status)
	}
	return status
}
