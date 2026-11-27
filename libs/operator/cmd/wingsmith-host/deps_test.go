package main

import (
	"os/exec"
	"strings"
	"testing"
)

// TestHostLinksNoKubernetes: the host binary runs where there is no API server, and client-go
// would add a dependency tree it never calls. The split into observe/kube and checkpoint/crdstore
// is what keeps it out; this keeps it split.
func TestHostLinksNoKubernetes(t *testing.T) {
	for _, env := range [][]string{{"GOOS=linux", "CGO_ENABLED=1"}, {"GOOS=linux", "CGO_ENABLED=0"}} {
		cmd := exec.Command("go", "list", "-deps", ".")
		cmd.Env = append(cmd.Environ(), env...)
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("go list %v: %v", env, err)
		}
		for _, dep := range strings.Fields(string(out)) {
			if strings.HasPrefix(dep, "k8s.io/") || strings.HasPrefix(dep, "sigs.k8s.io/") {
				t.Fatalf("%v: wingsmith-host depends on %s", env, dep)
			}
		}
	}
}
