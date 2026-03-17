package fake

import (
	"encoding/json"
	"os"
	"time"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// state is the whole simulated control plane, persisted as one JSON document.
type state struct {
	Identity api.Identity  `json:"identity"`
	Clusters []api.Cluster `json:"clusters"`
	Pools    []api.Pool    `json:"pools"`
	Nodes    []api.Node    `json:"nodes"`
	Devices  []api.Device  `json:"devices"`
}

// load reads a fixture document, falling back to the built-in seed. The seed
// is relative to a fixed epoch offset from now so ages stay plausible without
// the fixture needing to be regenerated.
func load(path string) (*state, error) {
	if path == "" {
		return seed(), nil
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return seed(), nil
	}
	if err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "cannot read fake state at %s", path)
	}
	var st state
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "fake state at %s is not valid JSON", path)
	}
	return &st, nil
}

func ptr(t time.Time) *time.Time { return &t }

func seed() *state {
	now := time.Now().UTC()
	st := &state{
		Identity: api.Identity{
			User:    "operator",
			Email:   "operator@hangar.dev",
			Role:    "admin",
			Project: "default",
			Server:  "fake://local",
		},
		Clusters: []api.Cluster{
			{
				ID:           "clu_01hq0v8p2m",
				Name:         "iad-1",
				Region:       "us-east",
				Phase:        api.PhaseActive,
				Reachable:    true,
				AgentVersion: "0.4.2",
				Capabilities: []string{"nvlink", "rdma", "mig"},
				Labels:       map[string]string{"tier": "prod", "fabric": "nvswitch"},
				Nodes:        24,
				Deployments:  11,
				Devices: []api.DeviceSummary{
					{Class: "h100-80", Total: 192, Healthy: 190, Allocated: 168},
				},
				EnrolledAt: now.Add(-210 * 24 * time.Hour),
				LastSyncAt: ptr(now.Add(-9 * time.Second)),
			},
			{
				ID:           "clu_01hq0v9r4t",
				Name:         "sjc-1",
				Region:       "us-west",
				Phase:        api.PhaseActive,
				Reachable:    true,
				AgentVersion: "0.4.2",
				Capabilities: []string{"nvlink", "rdma"},
				Labels:       map[string]string{"tier": "prod"},
				Nodes:        16,
				Deployments:  7,
				Devices: []api.DeviceSummary{
					{Class: "h200-141", Total: 96, Healthy: 96, Allocated: 40},
					{Class: "l40s-48", Total: 32, Healthy: 31, Allocated: 12},
				},
				EnrolledAt: now.Add(-120 * 24 * time.Hour),
				LastSyncAt: ptr(now.Add(-14 * time.Second)),
			},
			{
				ID:           "clu_01hq0vb6xk",
				Name:         "ams-1",
				Region:       "eu-west",
				Phase:        api.PhaseDraining,
				Reachable:    true,
				AgentVersion: "0.4.1",
				Capabilities: []string{"nvlink"},
				Labels:       map[string]string{"tier": "prod", "residency": "eu"},
				Nodes:        8,
				Deployments:  2,
				Devices: []api.DeviceSummary{
					{Class: "a100-80", Total: 64, Healthy: 60, Allocated: 8},
				},
				EnrolledAt: now.Add(-400 * 24 * time.Hour),
				LastSyncAt: ptr(now.Add(-3 * time.Minute)),
			},
			{
				ID:           "clu_01hq0vcd91",
				Name:         "sin-1",
				Region:       "ap-southeast",
				Phase:        api.PhasePending,
				Reachable:    false,
				AgentVersion: "",
				Labels:       map[string]string{"tier": "staging"},
				Nodes:        0,
				Deployments:  0,
				EnrolledAt:   now.Add(-40 * time.Minute),
			},
			{
				ID:          "clu_01hq0vdf27",
				Name:        "lab-0",
				Region:      "us-east",
				Phase:       api.PhaseDisabled,
				Reachable:   false,
				Labels:      map[string]string{"tier": "lab"},
				Nodes:       2,
				Deployments: 0,
				Devices: []api.DeviceSummary{
					{Class: "l4-24", Total: 8, Healthy: 8, Allocated: 0},
				},
				EnrolledAt: now.Add(-30 * 24 * time.Hour),
				LastSyncAt: ptr(now.Add(-9 * 24 * time.Hour)),
			},
		},
	}
	st.Pools, st.Nodes, st.Devices = buildSupply(now, st.Clusters)
	return st
}
