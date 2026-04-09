// Package fake is a local, deterministic stand-in for the Hangar control
// plane. It answers with the same shapes the real API answers with — same
// field names, same envelope, same cursor behaviour — so a command tested
// against it is a command tested for real shape.
//
// Select it with --fake, HANGAR_FAKE=1, or `fake: true` in the config file.
package fake

import (
	"encoding/json"
	"os"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// state is the whole simulated control plane, persisted as one JSON document
// whose keys are the API's collection names.
type state struct {
	Clusters        []api.Cluster            `json:"clusters"`
	Nodes           []api.Node               `json:"nodes"`
	Devices         []api.Device             `json:"devices"`
	TopologyDomains []api.TopologyDomain     `json:"topology_domains"`
	Pools           []api.Pool               `json:"pools"`
	Artifacts       []api.Artifact           `json:"artifacts"`
	Recipes         []api.Recipe             `json:"recipes"`
	Deployments     []api.Deployment         `json:"deployments"`
	Discovered      []api.DiscoveredWorkload `json:"discovered"`
	Endpoints       []api.Endpoint           `json:"endpoints"`
	Tenants         []api.Tenant             `json:"tenants"`
	Projects        []api.Project            `json:"projects"`
	Quotas          []api.Quota              `json:"quotas"`
	PriorityClasses []api.PriorityClass      `json:"priority_classes"`
	Policies        []api.Policy             `json:"policies"`
	Allocations     []api.Allocation         `json:"allocations"`
	Operations      []api.Operation          `json:"operations"`
	Components      []api.Component          `json:"components"`
	Events          []api.Event              `json:"events"`
}

// load reads a fixture document, falling back to the built-in seed. When a
// path is set but empty on disk the seed is written to it, so that a sequence
// of commands — create an operation, poll it, cancel it — shares one control
// plane instead of a fresh universe per invocation.
func load(path string) (*state, error) {
	if path == "" {
		return seed(), nil
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		st := seed()
		return st, save(path, st)
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

// save persists mutated state. A fake that forgets what it was told is a fake
// that cannot answer "is the drain done yet".
func save(path string, st *state) error {
	if path == "" {
		return nil
	}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot encode fake state")
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot write fake state at %s", path)
	}
	return nil
}
