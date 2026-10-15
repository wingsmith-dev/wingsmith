package cli

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
)

// --project on workload create is the global flag, so HANGAR_PROJECT and the
// context's project reach it too, and nothing defaults it to a project that was
// never named.
func TestWorkloadCreateTakesTheProjectFromAnywhereItCanBeSet(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	base := []string{"workload", "create", "assistant", "--artifact", "llama-3.1-70b-instruct",
		"--class", "h100-80", "--context-length", "8192", "-o", "json"}

	t.Setenv("HANGAR_PROJECT", "")
	_, errOut, code := run(t, base...)
	if code != 2 || !strings.Contains(errOut, "--project") {
		t.Fatalf("no project anywhere exited %d: %s", code, errOut)
	}

	t.Setenv("HANGAR_PROJECT", "core")
	out, errOut, code := run(t, base...)
	if code != 0 {
		t.Fatalf("HANGAR_PROJECT was not used: exit %d %s", code, errOut)
	}
	var wl map[string]any
	if err := json.Unmarshal([]byte(out), &wl); err != nil {
		t.Fatal(err)
	}
	if wl["project_id"] == "" || wl["name"] != "assistant" {
		t.Errorf("created %v", wl)
	}

	t.Setenv("HANGAR_PROJECT", "")
	if _, errOut, code := run(t, append([]string{"--project", "core"}, base...)...); code != 0 && !strings.Contains(errOut, "already exists") {
		t.Fatalf("--project before the verb was ignored: exit %d %s", code, errOut)
	}
}
