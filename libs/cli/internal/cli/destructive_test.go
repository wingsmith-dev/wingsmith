package cli

import (
	"path/filepath"
	"strings"
	"testing"
)

// A verb that changes or destroys something matches a whole id or name. `get`
// still takes a prefix; `rm` deleting the only thing that starts with "ch" is a
// guess, and it is the expensive kind.
func TestDestructiveVerbsNeverResolveByPrefix(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	if _, _, code := run(t, "workload", "get", "ch"); code != 0 {
		t.Fatalf("get by a unique prefix exited %d, want 0", code)
	}
	for _, args := range [][]string{
		{"workload", "rm", "ch", "--yes"},
		{"workload", "scale", "ch", "3", "--yes"},
		{"workload", "pause", "ch", "--yes"},
		{"workload", "drain", "chat", "--cluster", "sj", "--yes"},
		{"serving-quota", "rm", "ch", "--yes"},
		{"serving-quota", "set", "ch", "--committed-rpm", "5"},
	} {
		_, errOut, code := run(t, args...)
		if code != 4 {
			t.Errorf("hgr %s exited %d, want 4 (NOT_FOUND): %s", strings.Join(args, " "), code, errOut)
		}
	}
	if _, _, code := run(t, "workload", "get", "chat"); code != 0 {
		t.Fatal("a prefix-addressed rm deleted the workload anyway")
	}
	if _, errOut, code := run(t, "workload", "rm", "chat", "--yes"); code != 0 {
		t.Fatalf("rm by the full name exited %d: %s", code, errOut)
	}
}
