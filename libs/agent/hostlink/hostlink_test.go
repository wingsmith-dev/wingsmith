package hostlink

import (
	"errors"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/agent/internal/forward"
)

func TestRoutesResolveLoopbackPerDeployment(t *testing.T) {
	r := NewRoutes()
	if _, err := r.URLFor("dep_a"); !errors.Is(err, forward.ErrNoFrontend) {
		t.Fatalf("empty table: %v, want ErrNoFrontend", err)
	}
	r.Set(map[string][]int{"dep_a": {41001, 41002}, "dep_b": nil})
	seen := map[string]bool{}
	for range 4 {
		u, err := r.URLFor("dep_a")
		if err != nil {
			t.Fatal(err)
		}
		seen[u.Host] = true
	}
	if !seen["127.0.0.1:41001"] || !seen["127.0.0.1:41002"] || len(seen) != 2 {
		t.Fatalf("replicas used: %v", seen)
	}
	if _, err := r.URLFor("dep_b"); !errors.Is(err, forward.ErrNoDeploymentFrontend) {
		t.Fatalf("unready deployment: %v, want ErrNoDeploymentFrontend", err)
	}
	if _, err := r.URL(); !errors.Is(err, forward.ErrNoFrontend) {
		t.Fatalf("shared frontend on a host: %v", err)
	}
}
