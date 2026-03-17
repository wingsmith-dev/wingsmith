// Package fake is a local, deterministic stand-in for the Hangar control
// plane. It exists so the CLI is genuinely usable — and testable — before the
// API contract lands: every command works with zero network access.
//
// Select it with --fake, HANGAR_FAKE=1, or `fake: true` in the config file.
package fake

import (
	"context"
	"os"
	"sort"
	"strings"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// Backend implements api.Backend against seeded in-memory state.
type Backend struct {
	state *state
	fail  string
}

// New builds a fake backend from the built-in seed. A path may point at a
// JSON document that replaces the seed, which is how tests pin fixtures.
func New(path string) (*Backend, error) {
	st, err := load(path)
	if err != nil {
		return nil, err
	}
	return &Backend{state: st, fail: os.Getenv("HANGAR_FAKE_FAIL")}, nil
}

func (b *Backend) Simulated() bool { return true }

// injected turns HANGAR_FAKE_FAIL=<CODE> into a real failure, so error paths
// are exercisable without a server that can be made to misbehave.
func (b *Backend) injected() error {
	if b.fail == "" {
		return nil
	}
	code := errs.ParseCode(b.fail)
	if _, ok := errs.Explain(code); !ok {
		code = errs.CodeServerError
	}
	return errs.New(code, "injected failure (HANGAR_FAKE_FAIL=%s)", b.fail)
}

// Whoami returns the simulated identity.
func (b *Backend) Whoami(ctx context.Context) (api.Identity, error) {
	if err := b.injected(); err != nil {
		return api.Identity{}, err
	}
	return b.state.Identity, nil
}

// Clusters lists the seeded clusters, applying the same filters the plane is
// expected to apply server-side.
func (b *Backend) Clusters(ctx context.Context, opts api.ListOpts) ([]api.Cluster, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	var out []api.Cluster
	for _, c := range b.state.Clusters {
		if !opts.All && c.Phase == api.PhaseDisabled {
			continue
		}
		if opts.Status != "" && !strings.EqualFold(c.Phase, opts.Status) {
			continue
		}
		if opts.Region != "" && !strings.EqualFold(c.Region, opts.Region) {
			continue
		}
		if opts.Search != "" && !matches(c, opts.Search) {
			continue
		}
		if !hasLabels(c, opts.Labels) {
			continue
		}
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if opts.Limit > 0 && len(out) > opts.Limit {
		out = out[:opts.Limit]
	}
	return out, nil
}

// Cluster resolves a cluster by exact ID, exact name, or unique ID prefix.
func (b *Backend) Cluster(ctx context.Context, ref string) (api.Cluster, error) {
	if err := b.injected(); err != nil {
		return api.Cluster{}, err
	}
	if strings.TrimSpace(ref) == "" {
		return api.Cluster{}, errs.New(errs.CodeUsage, "a cluster name or id is required")
	}
	var hits []api.Cluster
	for _, c := range b.state.Clusters {
		if c.ID == ref || c.Name == ref {
			return c, nil
		}
		if strings.HasPrefix(c.ID, ref) || strings.HasPrefix(c.Name, ref) {
			hits = append(hits, c)
		}
	}
	switch len(hits) {
	case 1:
		return hits[0], nil
	case 0:
		return api.Cluster{}, errs.New(errs.CodeNotFound, "no cluster named %q", ref).
			WithFix("List clusters", "hgr cluster ls")
	default:
		names := make([]string, 0, len(hits))
		for _, h := range hits {
			names = append(names, h.Name)
		}
		return api.Cluster{}, errs.New(errs.CodeAmbiguous, "%q matches %d clusters", ref, len(hits)).
			WithCause(strings.Join(names, ", "))
	}
}

func matches(c api.Cluster, q string) bool {
	q = strings.ToLower(q)
	return strings.Contains(strings.ToLower(c.Name), q) ||
		strings.Contains(strings.ToLower(c.ID), q) ||
		strings.Contains(strings.ToLower(c.Region), q)
}

func hasLabels(c api.Cluster, want map[string]string) bool {
	for k, v := range want {
		if c.Labels[k] != v {
			return false
		}
	}
	return true
}
