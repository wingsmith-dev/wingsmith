package fake

import (
	"context"
	"testing"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

func newTestBackend(t *testing.T) *Backend {
	t.Helper()
	b, err := New("")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return b
}

func TestClustersHideDisabledByDefault(t *testing.T) {
	b := newTestBackend(t)
	got, err := b.Clusters(context.Background(), api.ListOpts{})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range got {
		if c.Phase == api.PhaseDisabled {
			t.Fatalf("disabled cluster %s returned without All", c.Name)
		}
	}
	all, err := b.Clusters(context.Background(), api.ListOpts{All: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(all) <= len(got) {
		t.Fatalf("All=true returned %d, not more than %d", len(all), len(got))
	}
}

func TestClustersSortedAndFiltered(t *testing.T) {
	b := newTestBackend(t)
	got, err := b.Clusters(context.Background(), api.ListOpts{Labels: map[string]string{"tier": "prod"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("label filter returned %d clusters, want 3", len(got))
	}
	for i := 1; i < len(got); i++ {
		if got[i-1].Name > got[i].Name {
			t.Fatalf("results not sorted by name: %v", got)
		}
	}
}

func TestClusterAmbiguousPrefix(t *testing.T) {
	b := newTestBackend(t)
	_, err := b.Cluster(context.Background(), "clu_01hq0v")
	if code := errs.As(err).Code; code != errs.CodeAmbiguous {
		t.Fatalf("got %v, want AMBIGUOUS", code)
	}
}

func TestInjectedFailure(t *testing.T) {
	t.Setenv("HANGAR_FAKE_FAIL", "RATE_LIMITED")
	b := newTestBackend(t)
	_, err := b.Clusters(context.Background(), api.ListOpts{})
	if code := errs.As(err).Code; code != errs.CodeRateLimited {
		t.Fatalf("got %v, want RATE_LIMITED", code)
	}
}

func TestDeviceTotals(t *testing.T) {
	b := newTestBackend(t)
	c, err := b.Cluster(context.Background(), "sjc-1")
	if err != nil {
		t.Fatal(err)
	}
	tot := c.DeviceTotals()
	if tot.Total != 128 || tot.Allocated != 52 {
		t.Fatalf("totals = %+v", tot)
	}
}
