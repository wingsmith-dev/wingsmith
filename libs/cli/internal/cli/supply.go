package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/ui"
)

func clusterNoun() noun[api.Cluster] {
	return noun[api.Cluster]{
		name:    "cluster",
		aliases: []string{"clusters"},
		group:   "supply",
		short:   "Enrolled GPU clusters and the state they report",
		long: `A cluster is one Kubernetes cluster enrolled with the plane.

The plane holds its enrolment record, region and capabilities; the operator
running in the cluster connects out and reports back. phase is what was
declared; reachable and sync are what the plane observes now. The two are shown
side by side.`,
		example: `  wings cluster ls
  wings cluster ls --region us-east-1 --phase active
  wings cluster ls --all -o json | jq '.items[].name'
  wings cluster get iad-1`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Cluster], error) {
			return b.Clusters(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Cluster, error) { return b.Cluster(ctx, ref) },
		keys:  func(c api.Cluster) []string { return []string{c.Name, c.ID} },
		empty: "no clusters enrolled",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "region"},
			{Title: "phase"},
			{Title: "reachable"},
			{Title: "sync"},
			{Title: "operator", Wide: true},
			{Title: "lag", Align: ui.AlignRight, Wide: true},
			{Title: "synced", Align: ui.AlignRight},
		},
		row: func(s ui.Style, c api.Cluster) []string {
			return []string{
				c.Name, c.ID, c.Region, clusterPhase(s, c), yesNo(s, c.Reachable),
				syncState(s, c.Sync.State), orDash(str(c.Sync.OperatorVersion)),
				lagCell(c.Sync.LagMS), ago(c.Sync.LastSyncAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.region, "region", "", "filter by region")
			cmd.Flags().StringVar(&f.phase, "phase", "", "filter by declared phase: pending|active|draining|disabled")
			f.bindSearch(cmd, "substring match on name or region")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, c api.Cluster) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(c.Name), s.Dim(c.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("display name", orDash(str(c.DisplayName)))
			kv.Add("region", c.Region)
			kv.Add("zone", orDash(str(c.Zone)))
			kv.Add("residency", orDash(str(c.Residency)))
			kv.Add("metrics label", orDash(str(c.MetricsLabel)))
			kv.Add("phase", clusterPhase(s, c))
			kv.Add("capabilities", orDash(capabilityString(c.Capabilities)))
			kv.Add("labels", labelString(c.Labels))
			kv.Add("enrolled", ago(c.EnrolledAt))
			kv.Add("revision", c.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			okv := app.KV()
			okv.Add("reachable", yesNo(s, c.Reachable))
			okv.Add("sync state", syncState(s, c.Sync.State))
			okv.Add("operator", orDash(str(c.Sync.OperatorVersion)))
			okv.Add("lag", lagCell(c.Sync.LagMS))
			okv.Add("last sync", ago(c.Sync.LastSyncAt))
			okv.Add("last seen", ago(c.Sync.LastSeenAt))
			okv.Add("last error", orDash(str(c.Sync.LastError)))
			okv.Render(w)
		},
		next: func(c api.Cluster) []errs.Action {
			return []errs.Action{
				{Description: "Nodes here", Command: "wings node ls --cluster " + c.Name},
				{Description: "Devices here", Command: "wings device ls --cluster " + c.Name},
				{Description: "What is running here", Command: "wings deployment ls --cluster " + c.Name},
			}
		},
	}
}

func lagCell(ms *int) string {
	if ms == nil {
		return "-"
	}
	if *ms < 1000 {
		return fmt.Sprintf("%dms", *ms)
	}
	return fmt.Sprintf("%ds", *ms/1000)
}

func nodeNoun() noun[api.Node] {
	return noun[api.Node]{
		name:    "node",
		aliases: []string{"nodes"},
		group:   "supply",
		short:   "Cluster members and the conditions they report",
		long: `A node is one machine a cluster's operator reports.

Nodes are observed, not declared. cordoned is shown beside the condition: a
drain or cordon operation sets it.

The device counts here are a rollup; wings device ls has the per-device detail.`,
		example: `  wings node ls --cluster iad-1
  wings node ls --pool h100-80-prod --condition ready
  wings node ls --cordoned -o name
  wings node ls --all --limit 200`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Node], error) {
			return b.Nodes(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Node, error) { return b.Node(ctx, ref) },
		keys:  func(n api.Node) []string { return []string{n.Name, n.ID} },
		empty: "no nodes match",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "cluster"},
			{Title: "pool"},
			{Title: "condition"},
			{Title: "ready"},
			{Title: "cordoned"},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "unhealthy", Align: ui.AlignRight},
			{Title: "operator", Wide: true},
			{Title: "seen", Align: ui.AlignRight},
		},
		row: func(s ui.Style, n api.Node) []string {
			return []string{
				n.Name, n.ID, n.ClusterName, orDash(str(n.PoolName)),
				conditionColor(s, n.Condition), boolWord(n.Ready), warnIf(s, n.Cordoned),
				strconv.Itoa(n.Devices.Total), countCell(s, n.Devices.Unhealthy()),
				orDash(str(n.OperatorVersion)), ago(n.ObservedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster (name, id, or unique prefix)")
			cmd.Flags().StringVar(&f.pool, "pool", "", "limit to one pool (name or id)")
			cmd.Flags().StringVar(&f.condition, "condition", "", "filter by condition: ready|not_ready|unknown")
			cmd.Flags().BoolVar(&f.ready, "ready", false, "only ready nodes (--ready=false for the rest)")
			cmd.Flags().BoolVar(&f.cordoned, "cordoned", false, "only cordoned nodes (--cordoned=false for the rest)")
			f.bindSearch(cmd, "substring match on name, id, cluster, or pool")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, n api.Node) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(n.Name), s.Dim(n.ID))
			kv := app.KV()
			kv.Add("cluster", n.ClusterName)
			kv.Add("pool", orDash(str(n.PoolName)))
			kv.Add("condition", conditionColor(s, n.Condition))
			kv.Add("ready", yesNo(s, n.Ready))
			kv.Add("cordoned", warnIf(s, n.Cordoned))
			kv.Add("machine", orDash(str(n.InstanceType)))
			kv.Add("cloud", orDash(str(n.Cloud)))
			kv.Add("zone", orDash(str(n.Zone)))
			kv.Add("cpu", num(n.CPUCores)+" cores "+orDash(str(n.CPUArch)))
			kv.Add("memory", mibPtr(n.MemoryMiB))
			kv.Add("os image", orDash(str(n.OSImage)))
			kv.Add("kernel", orDash(str(n.KernelVersion)))
			kv.Add("driver", orDash(str(n.DriverVersion)))
			kv.Add("operator", orDash(str(n.OperatorVersion)))
			kv.Add("labels", labelString(n.Labels))
			kv.Addf("taints", "%d", len(n.Taints))
			kv.Add("joined", ago(n.JoinedAt))
			kv.Add("last seen", ago(n.ObservedAt))
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("devices"))
			t := app.Table(ui.Column{Title: "health"}, ui.Column{Title: "count", Align: ui.AlignRight})
			for _, health := range []string{api.HealthHealthy, api.HealthDegraded, api.HealthUnhealthy, api.HealthUnknown} {
				if count, ok := n.Devices.ByHealth[health]; ok {
					t.Row(healthColor(s, health), strconv.Itoa(count))
				}
			}
			t.Row(s.Dim("total"), strconv.Itoa(n.Devices.Total))
			t.Render(w)
		},
		next: func(n api.Node) []errs.Action {
			return []errs.Action{
				{Description: "Devices on this node", Command: "wings device ls --node " + n.Name},
				{Description: "Drain it", Command: "wings operation create node.drain " + n.Name + " --reason firmware"},
			}
		},
	}
}

func deviceNoun() noun[api.Device] {
	return noun[api.Device]{
		name:    "device",
		aliases: []string{"devices", "gpu"},
		group:   "supply",
		short:   "Individual accelerators: the schedulable unit",
		long: `A device is one accelerator, the unit placement schedules.

Each device shows its health, ECC and throttle state, memory, MIG profile,
whether it can be allocated, and its topology domain.

Utilization, temperature and throughput are metrics and are not shown here.`,
		example: `  wings device ls --cluster iad-1
  wings device ls --class h100-80 --health unhealthy
  wings device ls --allocatable -o name
  wings device ls --cluster iad-1 --group-by domain`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Device], error) {
			return b.Devices(ctx, o)
		},
		get:    func(ctx context.Context, b api.Backend, ref string) (api.Device, error) { return b.Device(ctx, ref) },
		keys:   func(d api.Device) []string { return []string{d.ID, d.DeviceUUID} },
		empty:  "no devices match",
		getArg: "<device|uuid>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "uuid", Wide: true},
			{Title: "cluster"},
			{Title: "node"},
			{Title: "idx", Align: ui.AlignRight},
			{Title: "class"},
			{Title: "mem", Align: ui.AlignRight},
			{Title: "health"},
			{Title: "allocatable"},
			{Title: "domain"},
			{Title: "mig", Wide: true},
			{Title: "ecc", Wide: true},
		},
		row: func(s ui.Style, d api.Device) []string {
			return []string{
				d.ID, d.DeviceUUID, d.ClusterName, d.NodeName, strconv.Itoa(d.DeviceIndex),
				d.DeviceClass, mib(d.MemoryMiB), healthColor(s, d.Health), yesNo(s, d.Allocatable),
				domainCell(d), migCell(d), eccCell(d),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.node, "node", "", "limit to one node")
			cmd.Flags().StringVar(&f.pool, "pool", "", "limit to one pool")
			cmd.Flags().StringVar(&f.class, "class", "", "filter by device class, e.g. h100-80")
			cmd.Flags().StringVar(&f.health, "health", "", "filter by health: healthy|degraded|unhealthy|unknown")
			cmd.Flags().BoolVar(&f.allocatable, "allocatable", false, "only allocatable devices (--allocatable=false for the rest)")
			cmd.Flags().StringVar(&f.domain, "domain", "", "filter by topology domain id or identifier")
			cmd.Flags().StringVar(&f.groupBy, "group-by", "", "group the listing: domain|node|class")
		},
		detail: func(app *App, w io.Writer, s ui.Style, d api.Device) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(d.ID), s.Dim(d.DeviceUUID))
			kv := app.KV()
			kv.Add("cluster", d.ClusterName)
			kv.Addf("node", "%s (index %d)", d.NodeName, d.DeviceIndex)
			kv.Add("class", d.DeviceClass)
			kv.Add("vendor", d.Vendor)
			kv.Add("model", orDash(str(d.ModelName)))
			kv.Add("pci", orDash(str(d.PCIAddress)))
			kv.Add("memory", mib(d.MemoryMiB))
			kv.Add("compute", orDash(str(d.ComputeCapability)))
			kv.Add("health", healthColor(s, d.Health))
			if r := str(d.HealthReason); r != "" {
				kv.Add("reason", r)
			}
			kv.Add("allocatable", yesNo(s, d.Allocatable))
			kv.Addf("ecc", "enabled %s · %d volatile · %d aggregate · retirement %s",
				boolWord(d.ECCEnabled), d.ECCVolatileErrors, d.ECCAggregateErrors, boolWord(d.ECCPendingRetire))
			kv.Add("throttled", throttleCell(s, d))
			kv.Add("mig", migCell(d))
			if d.Topology != nil {
				kv.Addf("topology", "%s %s", d.Topology.Identifier, s.Dim("("+d.Topology.Kind+")"))
			} else {
				kv.Add("topology", "-")
			}
			kv.Add("last seen", ago(d.ObservedAt))
			kv.Render(w)
		},
		groupKey: func(by string, d api.Device) (string, string, error) {
			switch by {
			case "domain", "topology":
				return domainCell(d), topologyKind(d), nil
			case "node":
				return d.NodeName, "", nil
			case "class":
				return d.DeviceClass, "", nil
			}
			return "", "", errs.New(errs.CodeUsage, "cannot group devices by %q", by).
				WithCause("valid groupings: domain, node, class")
		},
		tally: func(g *listGroup, d api.Device) {
			if d.Health == api.HealthHealthy {
				g.Healthy++
			}
			if d.Allocatable {
				g.Allocatable++
			}
		},
		next: func(d api.Device) []errs.Action {
			out := []errs.Action{
				{Description: "Its node", Command: "wings node get " + d.NodeName},
				{Description: "What holds it", Command: "wings allocation ls --device " + d.ID},
			}
			if d.Topology != nil {
				out = append(out, errs.Action{Description: "Siblings in the same fabric", Command: "wings device ls --domain " + d.Topology.ID})
			}
			return out
		},
	}
}

func deviceClassNoun() noun[api.DeviceClass] {
	return noun[api.DeviceClass]{
		name:     "device-class",
		aliases:  []string{"device-classes", "class", "classes", "gpu-model"},
		plurally: "device classes",
		group:    "supply",
		short:    "The GPU model catalog and how many of each are installed",
		long: `A device class names a GPU model for placement, for example h100-80.

It is the string on a pool, a workload's shape, a quota and a device. The
catalog lives in libs/packages/contract/devices.json and is shared with the
operator, so a class is added by a release, not over the API.

The listing combines the catalog with what clusters report. A class with no
devices is in the catalog but not installed anywhere. A class marked
"catalog no" is installed but missing from the catalog, and should be added.`,
		example: `  wings device-class ls
  wings device-class ls --present
  wings device-class ls --cluster iad-1
  wings device-class get h100-80`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.DeviceClass], error) {
			return b.DeviceClasses(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.DeviceClass, error) {
			return b.DeviceClass(ctx, ref)
		},
		keys:   func(c api.DeviceClass) []string { return []string{c.Class} },
		empty:  "no device classes match",
		getArg: "<class>",
		columns: []ui.Column{
			{Title: "class"},
			{Title: "label"},
			{Title: "arch"},
			{Title: "mem", Align: ui.AlignRight},
			{Title: "form", Wide: true},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "impaired", Align: ui.AlignRight},
			{Title: "free", Align: ui.AlignRight},
			{Title: "nodes", Align: ui.AlignRight},
			{Title: "clusters"},
			{Title: "catalog"},
		},
		row: func(s ui.Style, c api.DeviceClass) []string {
			return []string{
				c.Class, orDash(c.Label), orDash(c.Architecture), mib(c.MemoryMiB), orDash(c.FormFactor),
				strconv.Itoa(c.Inventory.Devices), countCell(s, c.Inventory.Devices-c.Inventory.Healthy),
				strconv.Itoa(c.Inventory.Allocatable), strconv.Itoa(c.Inventory.Nodes),
				orDash(strings.Join(c.Inventory.Clusters, ",")), yesNo(s, c.InCatalog),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "count only one cluster's devices")
			cmd.Flags().StringVar(&f.arch, "architecture", "", "filter by vendor generation, e.g. hopper")
			cmd.Flags().BoolVar(&f.present, "present", false, "only classes the fleet holds (--present=false for the rest)")
		},
		detail: func(app *App, w io.Writer, s ui.Style, c api.DeviceClass) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(c.Class), s.Dim(c.Label))
			kv := app.KV()
			kv.Add("architecture", orDash(c.Architecture))
			kv.Add("vendor", orDash(c.Vendor))
			kv.Add("memory", mib(c.MemoryMiB))
			kv.Add("form factor", orDash(c.FormFactor))
			kv.Add("in catalog", yesNo(s, c.InCatalog))
			if !c.InCatalog {
				kv.Add("", s.Dim("not in devices.json: add it there and release both halves"))
			}
			kv.Addf("devices", "%d total · %d healthy · %d free · %d allocated",
				c.Inventory.Devices, c.Inventory.Healthy, c.Inventory.Allocatable, c.Inventory.Allocated)
			kv.Addf("spread", "%s · %s", countWord(c.Inventory.Nodes, "node"), orDash(strings.Join(c.Inventory.Clusters, ", ")))
			for dialect, names := range c.Aliases {
				kv.Addf("alias "+dialect, "%s", strings.Join(names, ", "))
			}
			kv.Render(w)
		},
		next: func(c api.DeviceClass) []errs.Action {
			return []errs.Action{
				{Description: "Devices of this class", Command: "wings device ls --class " + c.Class},
				{Description: "Pools that provision it", Command: "wings pool ls --class " + c.Class},
				{Description: "Workloads that ask for it", Command: "wings workload ls --class " + c.Class},
			}
		},
	}
}

func domainCell(d api.Device) string {
	if d.Topology == nil {
		return "-"
	}
	return d.Topology.Identifier
}

func migCell(d api.Device) string {
	if !d.MIGEnabled {
		return "-"
	}
	return orDash(str(d.MIGProfile))
}

func eccCell(d api.Device) string {
	return fmt.Sprintf("%d/%d", d.ECCVolatileErrors, d.ECCAggregateErrors)
}

func throttleCell(s ui.Style, d api.Device) string {
	if !d.Throttled {
		return s.Dim("no")
	}
	if len(d.ThrottleReasons) == 0 {
		return s.Amber("yes")
	}
	return s.Amber("yes") + " " + s.Dim(joinAll(d.ThrottleReasons))
}

func topologyNoun() noun[api.TopologyDomain] {
	return noun[api.TopologyDomain]{
		name:    "topology-domain",
		aliases: []string{"topology-domains", "domain", "domains"},
		group:   "supply",
		short:   "Sets of devices that share an interconnect",
		long: `A topology domain is a group of devices that share a fabric, such as one
NVLink island.

A device shows a summary of its own domain. Membership, nesting and bandwidth
are here, and are what placement checks before putting a tensor-parallel
workload on one domain.`,
		example: `  wings topology-domain ls --cluster iad-1
  wings topology-domain ls --kind nvswitch
  wings topology-domain ls --spans-nodes`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.TopologyDomain], error) {
			return b.TopologyDomains(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.TopologyDomain, error) {
			return b.TopologyDomain(ctx, ref)
		},
		keys:  func(t api.TopologyDomain) []string { return []string{t.Identifier, t.ID} },
		empty: "no topology domains match",
		columns: []ui.Column{
			{Title: "identifier"},
			{Title: "id", Wide: true},
			{Title: "cluster"},
			{Title: "nodes"},
			{Title: "kind"},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "gen", Wide: true},
			{Title: "gbps", Align: ui.AlignRight, Wide: true},
			{Title: "spans nodes"},
		},
		row: func(s ui.Style, t api.TopologyDomain) []string {
			return []string{
				t.Identifier, t.ID, t.ClusterName, listCell(t.NodeNames), t.Kind,
				strconv.Itoa(t.DeviceCount), orDash(str(t.LinkGeneration)), num(t.LinkBandwidthGbps),
				boolWord(t.SpansNodes),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.node, "node", "", "limit to one node")
			cmd.Flags().StringVar(&f.kind, "kind", "", "filter by fabric kind: nvswitch|nvlink|pcie|numa")
			cmd.Flags().BoolVar(&f.spansNodes, "spans-nodes", false, "only domains that span nodes")
		},
		detail: func(app *App, w io.Writer, s ui.Style, t api.TopologyDomain) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(t.Identifier), s.Dim(t.ID))
			kv := app.KV()
			kv.Add("cluster", t.ClusterName)
			kv.Add("nodes", listCell(t.NodeNames))
			kv.Add("parent", orDash(str(t.Parent)))
			kv.Add("kind", t.Kind)
			kv.Add("link generation", orDash(str(t.LinkGeneration)))
			kv.Addf("bandwidth", "%s Gbps", num(t.LinkBandwidthGbps))
			kv.Addf("devices", "%d", t.DeviceCount)
			kv.Add("spans nodes", boolWord(t.SpansNodes))
			kv.Add("attributes", labelString(t.Attributes))
			kv.Add("last seen", ago(t.ObservedAt))
			kv.Render(w)
		},
		next: func(t api.TopologyDomain) []errs.Action {
			return []errs.Action{{Description: "Its devices", Command: "wings device ls --domain " + t.ID}}
		},
	}
}

func poolNoun() noun[api.Pool] {
	return noun[api.Pool]{
		name:    "pool",
		aliases: []string{"pools"},
		group:   "supply",
		short:   "Declared groups of identical nodes",
		long: `A pool is a declared group of identical nodes in a cluster: a device class,
a node shape, and size bounds.

Pools give placement a set of matching devices to work with; a cluster with
mixed hardware has one pool per kind. Which nodes are in a pool is observed,
and is shown separately from the declaration.`,
		example: `  wings pool ls
  wings pool ls --cluster iad-1 --class h100-80
  wings pool get h100-80-prod`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Pool], error) {
			return b.Pools(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Pool, error) { return b.Pool(ctx, ref) },
		keys:  func(p api.Pool) []string { return []string{p.Name, p.ID} },
		empty: "no pools declared",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "cluster", Wide: true},
			{Title: "class"},
			{Title: "per node", Align: ui.AlignRight},
			{Title: "bounds", Align: ui.AlignRight},
			{Title: "nodes", Align: ui.AlignRight},
			{Title: "devices", Align: ui.AlignRight},
			{Title: "unhealthy", Align: ui.AlignRight},
			{Title: "enabled"},
			{Title: "in bounds"},
		},
		row: func(s ui.Style, p api.Pool) []string {
			m := p.Membership
			return []string{
				p.Name, p.ID, p.ClusterID, p.DeviceClass, strconv.Itoa(p.DevicesPerNode),
				fmt.Sprintf("%d-%d", p.MinNodes, p.MaxNodes),
				fmt.Sprintf("%d/%d", m.NodesReady, m.NodesTotal),
				fmt.Sprintf("%d/%d", m.DevicesAllocatable, m.DevicesTotal),
				countCell(s, m.DevicesTotal-m.DevicesHealthy),
				boolWord(p.Enabled), yesNo(s, p.WithinDeclaredBound),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.class, "class", "", "filter by device class")
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled pools (--enabled=false for the rest)")
			f.bindSearch(cmd, "substring match on name or device class")
			f.bindLabels(cmd)
		},
		detail: func(app *App, w io.Writer, s ui.Style, p api.Pool) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(p.Name), s.Dim(p.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("cluster", p.ClusterID)
			kv.Add("device class", p.DeviceClass)
			kv.Addf("devices/node", "%d", p.DevicesPerNode)
			kv.Addf("bounds", "%d-%d nodes", p.MinNodes, p.MaxNodes)
			kv.Add("node shape", labelString(p.NodeShape))
			kv.Add("labels", labelString(p.Labels))
			kv.Addf("taints", "%d", len(p.Taints))
			kv.Add("enabled", yesNo(s, p.Enabled))
			kv.Add("created", agoAt(p.CreatedAt))
			kv.Add("revision", p.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("observed"))
			m := p.Membership
			okv := app.KV()
			okv.Addf("nodes", "%d (%d ready, %d cordoned)", m.NodesTotal, m.NodesReady, m.NodesCordoned)
			okv.Addf("devices", "%d", m.DevicesTotal)
			okv.Addf("healthy", "%d", m.DevicesHealthy)
			okv.Addf("allocatable", "%d", m.DevicesAllocatable)
			okv.Add("within declared bounds", yesNo(s, p.WithinDeclaredBound))
			okv.Render(w)
		},
		next: func(p api.Pool) []errs.Action {
			return []errs.Action{
				{Description: "Nodes in this pool", Command: "wings node ls --pool " + p.Name},
				{Description: "Allocatable devices", Command: "wings device ls --pool " + p.Name + " --allocatable"},
			}
		},
	}
}

func topologyKind(d api.Device) string {
	if d.Topology == nil {
		return ""
	}
	return d.Topology.Kind
}

func joinAll(v []string) string { return strings.Join(v, " ") }
