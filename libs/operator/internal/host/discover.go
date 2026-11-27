package host

import (
	"bufio"
	"bytes"
	"context"
	"log/slog"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/devices"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
)

// Distribution is what a host reports in cluster.observed. The plane stores and displays it; the
// cluster's declared kind is what anything branches on.
const Distribution = "host"

// Machine is what the host is, apart from its accelerators. Read from /proc and /etc by default;
// tests supply it.
type Machine struct {
	Hostname      string
	KernelVersion string
	OSImage       string
	CPUCores      int
	MemoryMiB     int64
	CPUArch       string
}

// Discoverer is the host's observe.Discoverer: one node, this machine, with the accelerators the
// driver reports. Everything downstream of the snapshot (projection, change detection, resync) is
// the same observation loop the Kubernetes operator runs.
type Discoverer struct {
	Devices Source
	// Machine reads the machine facts. Nil reads them from the running system.
	Machine func() Machine
	// Labels are the operator-chosen labels placement matches on, from the host config.
	Labels map[string]string
	Pool   string
	// Runtimes are the serving runtimes this host runs: [container] once serving is on, nil
	// (unknown) otherwise.
	Runtimes []string
	Log      *slog.Logger
	Now      func() time.Time
}

var _ observe.Discoverer = (*Discoverer)(nil)

func (d *Discoverer) Discover(ctx context.Context) (observe.Snapshot, error) {
	machine := d.Machine
	if machine == nil {
		machine = ReadMachine
	}
	m := machine()
	node := observe.Node{
		Name:          m.Hostname,
		Pool:          d.Pool,
		Ready:         true,
		KernelVersion: m.KernelVersion,
		OSImage:       m.OSImage,
		CPUCores:      m.CPUCores,
		MemoryMiB:     m.MemoryMiB,
		CPUArch:       m.CPUArch,
		Labels:        d.Labels,
		Distribution:  Distribution,
	}
	inv, err := d.Devices.Read(ctx)
	if err != nil {
		// A host whose driver cannot be read is still a member, so it reports itself and no
		// devices instead of failing the tick, which would leave the plane with the last reading
		// as though it were current. The node is marked not ready so placement does not count on
		// it, and the reason is on the node, not only in a log line nobody reads.
		d.log().Warn("device inventory unavailable", "error", err)
		node.Ready = false
		node.Conditions = []observe.Condition{{
			Type: "devices_readable", Status: "false", Reason: "driver_unavailable",
			Message: truncate(err.Error(), 256), Since: d.now(),
		}}
	} else {
		node.DriverVersion = inv.DriverVersion
		node.Devices = d.devices(m.Hostname, inv.GPUs)
	}
	return observe.Snapshot{Nodes: []observe.Node{node}, Runtimes: d.Runtimes, At: d.now()}, nil
}

func (d *Discoverer) devices(hostname string, gpus []GPU) []observe.Device {
	table := devices.Load()
	out := make([]observe.Device, 0, len(gpus))
	for _, g := range gpus {
		class := table.Resolve(devices.DialectNVML, g.Name)
		memory := g.MemoryMiB
		if memory <= 0 {
			if entry, ok := table.ByClass(class); ok {
				memory = entry.MemoryMiB
			}
		}
		health := observe.HealthHealthy
		if g.VolatileUncorrected > 0 {
			health = observe.HealthUnhealthy
		}
		reasons := g.ThrottleReasons
		if reasons == nil {
			reasons = []string{}
		}
		out = append(out, observe.Device{
			UUID:          g.UUID,
			Index:         g.Index,
			Model:         g.Name,
			DeviceClass:   class,
			MemoryTotalMB: memory,
			Health:        health,
			Ecc: observe.Ecc{
				ModeEnabled:          g.ECCEnabled,
				VolatileCorrected:    g.VolatileCorrected,
				VolatileUncorrected:  g.VolatileUncorrected,
				AggregateUncorrected: g.AggregateUncorrected,
			},
			Throttled:       len(reasons) > 0,
			ThrottleReasons: reasons,
			Mig:             observe.Mig{Enabled: g.MIGEnabled},
			// NVLink topology is P2.4's to read; until then every card is its own host's PCIe
			// domain, which is what a single-node cluster can promise anyway.
			DomainKind: "pcie_root",
			DomainKey:  hostname,
		})
	}
	return out
}

func (d *Discoverer) now() time.Time {
	if d.Now != nil {
		return d.Now().UTC()
	}
	return time.Now().UTC()
}

func (d *Discoverer) log() *slog.Logger {
	if d.Log != nil {
		return d.Log
	}
	return slog.Default()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ReadMachine reads the running system. Fields it cannot read stay zero.
func ReadMachine() Machine {
	m := Machine{CPUCores: runtime.NumCPU(), CPUArch: runtime.GOARCH}
	m.Hostname, _ = os.Hostname()
	if raw, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
		m.KernelVersion = strings.TrimSpace(string(raw))
	}
	if raw, err := os.ReadFile("/etc/os-release"); err == nil {
		m.OSImage = osRelease(raw)
	}
	if raw, err := os.ReadFile("/proc/meminfo"); err == nil {
		m.MemoryMiB = memTotalMiB(raw)
	}
	return m
}

func osRelease(raw []byte) string {
	s := bufio.NewScanner(bytes.NewReader(raw))
	for s.Scan() {
		if v, ok := strings.CutPrefix(s.Text(), "PRETTY_NAME="); ok {
			return strings.Trim(v, `"'`)
		}
	}
	return ""
}

func memTotalMiB(raw []byte) int64 {
	s := bufio.NewScanner(bytes.NewReader(raw))
	for s.Scan() {
		if v, ok := strings.CutPrefix(s.Text(), "MemTotal:"); ok {
			kb, _ := strconv.ParseInt(strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(v), "kB")), 10, 64)
			return kb / 1024
		}
	}
	return 0
}
