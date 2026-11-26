package cli

import (
	"strings"
	"testing"
)

func TestGetPrintsAManifestAsYAML(t *testing.T) {
	t.Setenv("WINGSMITH_PROJECT", "core")
	out, errOut, code := run(t, "get", "workload", "chat", "-o", "yaml")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	for _, want := range []string{"apiVersion: plane.wingsmith.dev/v1alpha1", "kind: Workload", "name: chat"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in\n%s", want, out)
		}
	}
}

func TestGetRefusesANamespacedKindWithoutAProjectAndAnUnknownKind(t *testing.T) {
	t.Setenv("WINGSMITH_PROJECT", "")
	if _, errOut, code := run(t, "get", "endpoint", "chat", "--project", ""); code != 2 || !strings.Contains(errOut, "project") {
		t.Errorf("no project: exit %d %s", code, errOut)
	}
	if _, errOut, code := run(t, "get", "cluster", "iad"); code != 2 || !strings.Contains(errOut, "unknown kind") {
		t.Errorf("unknown kind: exit %d %s", code, errOut)
	}
}
