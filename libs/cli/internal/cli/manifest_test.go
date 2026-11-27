package cli

import (
	"os"
	"path/filepath"
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

func TestApplySendsWorkloadsBeforeEndpointsAndRefusesArtifacts(t *testing.T) {
	t.Setenv("WINGSMITH_PROJECT", "core")
	dir := t.TempDir()
	file := filepath.Join(dir, "m.yaml")
	doc := `apiVersion: plane.wingsmith.dev/v1alpha1
kind: Endpoint
metadata: {name: chat}
spec: {targets: [{workloadRef: {name: chat}}]}
---
apiVersion: plane.wingsmith.dev/v1alpha1
kind: Workload
metadata: {name: chat, namespace: core}
spec: {}
`
	if err := os.WriteFile(file, []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	out, errOut, code := run(t, "apply", "-f", file, "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	w, e := strings.Index(out, "workload/chat"), strings.Index(out, "endpoint/chat")
	if w < 0 || e < 0 || w > e {
		t.Errorf("want the workload applied first:\n%s", out)
	}

	if err := os.WriteFile(file, []byte(doc+"---\nkind: Artifact\nmetadata: {name: gpt}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, errOut, code := run(t, "apply", "-f", file); code != 2 || !strings.Contains(errOut, "cannot be applied yet") {
		t.Errorf("artifact: exit %d %s", code, errOut)
	}
	if _, errOut, code := run(t, "apply"); code != 2 || !strings.Contains(errOut, "file") {
		t.Errorf("no file: exit %d %s", code, errOut)
	}
}
