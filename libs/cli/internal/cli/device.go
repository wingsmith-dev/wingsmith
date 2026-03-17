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

func newDeviceCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "device",
		Aliases: []string{"devices", "gpu"},
		Short:   "Individual accelerators — the schedulable unit",
		Long: `A device is one accelerator, and it is what actually gets scheduled.

"Seven of eight healthy" is not something a node-level GPU count can say, so
every capacity question is answered here: per-device health, ECC and throttle
state, memory, MIG profile, the topology domain the device sits in, and what
currently holds it.

Topology is first-class. --domain filters by domain name, id, or fabric kind,
and --group-by domain groups the listing so "do I have eight devices in one
NVLink domain" is one command.`,
	}
	group(cmd, "supply")
	cmd.AddCommand(newDeviceListCmd(app), newDeviceGetCmd(app))
	return cmd
}

func newDeviceListCmd(app *App) *cobra.Command {
	f := &supplyFlags{}
	cmd := &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List accelerators",
		Example: `  hgr device ls --cluster iad-1
  hgr device ls --class h100-80 --status free
  hgr device ls --health degraded -o wide
  hgr device ls --cluster sjc-1 --group-by domain
  hgr device ls --domain nvlink --status free -o json | jq '.count'`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDeviceList(app, cmdPath(cmd), f)
		},
	}
	f.bindShared(cmd, "substring match on id, uuid, node, class, or deployment")
	f.bindCluster(cmd)
	f.bindClass(cmd)
	f.bindHealth(cmd)
	f.bindDomain(cmd)
	f.bindStatus(cmd, "filter by allocation: allocated|free")
	cmd.Flags().StringVar(&f.node, "node", "", "limit to one node (name, id, or unique prefix)")
	cmd.Flags().StringVar(&f.pool, "pool", "", "limit to one pool")
	cmd.Flags().StringVar(&f.groupBy, "group-by", "", "group the listing: domain|node|class")
	return cmd
}

// deviceGroup is one bucket of a grouped listing. It carries its own counts
// so a consumer never has to re-aggregate to answer a capacity question.
// Free is schedulable capacity — healthy and unallocated — not merely the
// absence of an allocation.
type deviceGroup struct {
	Key       string `json:"key"`
	Kind      string `json:"kind,omitempty"`
	Cluster   string `json:"cluster,omitempty"`
	Count     int    `json:"count"`
	Healthy   int    `json:"healthy"`
	Allocated int    `json:"allocated"`
	Free      int    `json:"free"`
}

// deviceList is the machine contract of `hgr device ls`. Groups appear only
// when --group-by was asked for; items are always present and always the
// complete result.
type deviceList struct {
	Items      []api.Device  `json:"items"`
	Count      int           `json:"count"`
	Groups     []deviceGroup `json:"groups,omitempty"`
	GroupBy    string        `json:"group_by,omitempty"`
	NextCursor string        `json:"next_cursor,omitempty"`
}

func runDeviceList(app *App, path string, f *supplyFlags) error {
	opts, err := f.opts()
	if err != nil {
		return err
	}
	groupBy, err := parseGroupBy(f.groupBy)
	if err != nil {
		return err
	}
	ctx, cancel := app.RequestContext()
	defer cancel()

	step := app.Report.Step("listing devices")
	devices, err := app.Backend.Devices(ctx, opts)
	if err != nil {
		step.Fail("failed")
		return err
	}
	step.Done("%d devices", len(devices))

	names := make([]string, 0, len(devices))
	rows := make([][]string, 0, len(devices))
	for _, d := range devices {
		names = append(names, d.ID)
		rows = append(rows, deviceRow(d))
	}

	data := deviceList{Items: devices, Count: len(devices), GroupBy: groupBy}
	if groupBy != "" {
		data.Groups = groupDevices(devices, groupBy)
	}

	var next []errs.Action
	if len(devices) > 0 {
		next = []errs.Action{{Description: "Inspect one", Command: "hgr device get " + devices[0].ID}}
		if groupBy == "" {
			next = append(next, errs.Action{Description: "Group by fabric", Command: "hgr device ls --group-by domain"})
		}
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    data,
		Names:   names,
		Columns: []string{"id", "uuid", "cluster", "node", "index", "class", "memory_gb", "health", "state", "deployment", "domain"},
		Rows:    rows,
		Human:   deviceHuman(app, devices, data.Groups, groupBy),
		Next:    next,
	})
}

func deviceRow(d api.Device) []string {
	return []string{
		d.ID, d.UUID, d.Cluster, d.Node, strconv.Itoa(d.Index), d.Class,
		strconv.Itoa(d.MemoryGB), d.Health, d.AllocationStatus(),
		allocationLabel(d), d.DomainName(),
	}
}

func deviceHuman(app *App, devices []api.Device, groups []deviceGroup, groupBy string) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(devices) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim("no devices match"))
			return
		}
		if groupBy == "" {
			deviceTable(app, devices).Render(w)
			fmt.Fprintf(w, "\n%s\n", s.Dim(deviceFooter(devices)))
			return
		}
		for i, g := range groups {
			if i > 0 {
				fmt.Fprintln(w)
			}
			head := g.Key
			if g.Kind != "" {
				head += " " + s.Dim("("+g.Kind+")")
			}
			fmt.Fprintf(w, "%s  %s\n", s.Bold(head),
				s.Dim(fmt.Sprintf("%d devices · %d healthy · %d allocated · %d free", g.Count, g.Healthy, g.Allocated, g.Free)))
			deviceTable(app, devicesIn(devices, groupBy, g.Key)).Render(w)
		}
		fmt.Fprintf(w, "\n%s\n", s.Dim(deviceFooter(devices)))
	}
}

func deviceTable(app *App, devices []api.Device) *ui.Table {
	t := app.Table(
		ui.Column{Title: "id"},
		ui.Column{Title: "uuid", Wide: true},
		ui.Column{Title: "cluster"},
		ui.Column{Title: "node"},
		ui.Column{Title: "idx", Align: ui.AlignRight},
		ui.Column{Title: "class"},
		ui.Column{Title: "mem", Align: ui.AlignRight},
		ui.Column{Title: "health"},
		ui.Column{Title: "state"},
		ui.Column{Title: "deployment"},
		ui.Column{Title: "domain"},
		ui.Column{Title: "mig", Wide: true},
		ui.Column{Title: "ecc", Wide: true},
	)
	for _, d := range devices {
		t.Row(
			d.ID, d.UUID, d.Cluster, d.Node, strconv.Itoa(d.Index), d.Class,
			memoryCell(d),
			healthColor(app.Style, d.Health),
			allocationColor(app.Style, d),
			allocationLabel(d),
			d.DomainName(),
			migCell(d),
			eccCell(d),
		)
	}
	return t
}

func deviceFooter(devices []api.Device) string {
	var healthy, allocated, free int
	for _, d := range devices {
		if d.Health == api.HealthHealthy {
			healthy++
			if !d.Allocated() {
				free++
			}
		}
		if d.Allocated() {
			allocated++
		}
	}
	return fmt.Sprintf("%d devices · %d healthy · %d allocated · %d free",
		len(devices), healthy, allocated, free)
}

func parseGroupBy(v string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "":
		return "", nil
	case "domain", "topology":
		return "domain", nil
	case "node":
		return "node", nil
	case "class":
		return "class", nil
	}
	return "", errs.New(errs.CodeUsage, "cannot group devices by %q", v).
		WithCause("valid groupings: domain, node, class")
}

func groupKey(d api.Device, by string) string {
	switch by {
	case "node":
		return d.Node
	case "class":
		return d.Class
	default:
		return d.DomainName()
	}
}

func groupDevices(devices []api.Device, by string) []deviceGroup {
	index := map[string]*deviceGroup{}
	var order []string
	for _, d := range devices {
		key := groupKey(d, by)
		g, ok := index[key]
		if !ok {
			g = &deviceGroup{Key: key, Cluster: d.Cluster}
			if by == "domain" && d.Topology != nil {
				g.Kind = d.Topology.Kind
			}
			index[key] = g
			order = append(order, key)
		}
		g.Count++
		if d.Health == api.HealthHealthy {
			g.Healthy++
		}
		if d.Allocated() {
			g.Allocated++
		} else if d.Health == api.HealthHealthy {
			g.Free++
		}
	}
	sort.Strings(order)
	out := make([]deviceGroup, 0, len(order))
	for _, k := range order {
		out = append(out, *index[k])
	}
	return out
}

func devicesIn(devices []api.Device, by, key string) []api.Device {
	var out []api.Device
	for _, d := range devices {
		if groupKey(d, by) == key {
			out = append(out, d)
		}
	}
	return out
}

func newDeviceGetCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "get <device>",
		Aliases: []string{"show", "describe"},
		Short:   "Show one device in detail",
		Long: `Resolve a device by id, GPU UUID, or unique prefix of either, and print
its identity, health, ECC and throttle state, MIG profile, topology domain,
and current allocation.`,
		Example: `  hgr device get dev_0a1b2c3d4e5
  hgr device get GPU-1a2b3c4d-… -o json
  hgr device get dev_0a1b -o jsonpath=.allocation.deployment`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDeviceGet(app, cmdPath(cmd), args[0])
		},
	}
}

func runDeviceGet(app *App, path, ref string) error {
	ctx, cancel := app.RequestContext()
	defer cancel()

	d, err := app.Backend.Device(ctx, ref)
	if err != nil {
		return err
	}

	return app.Emit(&output.Result{
		Command: path,
		Data:    d,
		Names:   []string{d.ID},
		Human: func(w io.Writer, s ui.Style, width int) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(d.ID), s.Dim(d.UUID))
			kv := app.KV()
			kv.Add("cluster", d.Cluster)
			kv.Addf("node", "%s (index %d)", d.Node, d.Index)
			kv.Add("pool", orDash(d.Pool))
			kv.Add("class", d.Class)
			kv.Add("model", orDash(d.Model))
			kv.Add("memory", memoryCell(d))
			kv.Add("health", healthColor(s, d.Health))
			if d.HealthReason != "" {
				kv.Add("reason", d.HealthReason)
			}
			kv.Addf("ecc", "%d corrected · %d uncorrected", d.ECCCorrected, d.ECCUncorrected)
			kv.Add("throttled", throttleCell(s, d))
			kv.Add("mig", migCell(d))
			if d.Topology != nil {
				kv.Addf("topology", "%s %s · %d devices", d.Topology.Name, s.Dim("("+d.Topology.Kind+")"), d.Topology.Devices)
			} else {
				kv.Add("topology", "-")
			}
			kv.Add("driver", orDash(d.Driver))
			kv.Add("last seen", lastSeen(d.LastSeenAt))
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("allocation"))
			if d.Allocation == nil {
				fmt.Fprintf(w, "  %s\n", s.Green("free"))
				return
			}
			akv := app.KV()
			akv.Add("deployment", d.Allocation.Deployment)
			akv.Add("replica", orDash(d.Allocation.Replica))
			akv.Add("project", orDash(d.Allocation.Project))
			akv.Addf("since", "%s ago", api.Age(d.Allocation.Since))
			akv.Render(w)
		},
		Next: []errs.Action{
			{Description: "Its node", Command: "hgr node get " + d.Node},
			{Description: "Siblings in the same fabric", Command: "hgr device ls --domain " + d.DomainName()},
		},
	})
}

func memoryCell(d api.Device) string {
	if d.MemoryUsedGB > 0 {
		return fmt.Sprintf("%d/%dGB", d.MemoryUsedGB, d.MemoryGB)
	}
	return fmt.Sprintf("%dGB", d.MemoryGB)
}

func migCell(d api.Device) string {
	if !d.MIGEnabled {
		return "-"
	}
	return orDash(d.MIGProfile)
}

func eccCell(d api.Device) string {
	return fmt.Sprintf("%d/%d", d.ECCCorrected, d.ECCUncorrected)
}

func throttleCell(s ui.Style, d api.Device) string {
	if !d.Throttled {
		return s.Dim("no")
	}
	if len(d.ThrottleReasons) == 0 {
		return s.Amber("yes")
	}
	return s.Amber("yes") + " " + s.Dim(strings.Join(d.ThrottleReasons, " "))
}

func healthColor(s ui.Style, health string) string {
	switch health {
	case api.HealthHealthy:
		return s.Green(health)
	case api.HealthDegraded:
		return s.Amber(health)
	case api.HealthUnhealthy:
		return s.Red(health)
	default:
		return s.Dim(health)
	}
}

func allocationColor(s ui.Style, d api.Device) string {
	if d.Allocated() {
		return s.Cyan("allocated")
	}
	return s.Green("free")
}

func allocationLabel(d api.Device) string {
	if d.Allocation == nil {
		return "-"
	}
	return d.Allocation.Deployment
}
