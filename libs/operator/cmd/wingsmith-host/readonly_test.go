package main

import (
	"context"
	"io"
	"log/slog"
	"os/exec"
	"strings"
	"testing"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

// TestRunAppliesNothing: until P2.4 a host is supply-only. Declared state is acknowledged and
// dropped, and nothing that could act on it is linked in.
func TestRunAppliesNothing(t *testing.T) {
	handle := ignoreDemand(slog.New(slog.NewTextHandler(io.Discard, nil)))
	for _, typ := range []string{"workload.desired", "operation.requested", "node.desired"} {
		if err := handle(context.Background(), &syncpkg.Envelope{Type: typ}); err != nil {
			t.Fatalf("%s: %v", typ, err)
		}
	}
	out, err := exec.Command("go", "list", "-deps", ".").Output()
	if err != nil {
		t.Fatal(err)
	}
	for _, dep := range strings.Fields(string(out)) {
		for _, banned := range []string{"/internal/operate", "/internal/reconcile", "/internal/render", "/internal/workload"} {
			if strings.HasSuffix(dep, banned) || strings.Contains(dep, banned+"/") {
				t.Fatalf("wingsmith-host links %s, which applies declared state", dep)
			}
		}
	}
}
