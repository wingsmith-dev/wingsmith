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
	t.Setenv("WINGSMITH_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
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
			t.Errorf("wings %s exited %d, want 4 (NOT_FOUND): %s", strings.Join(args, " "), code, errOut)
		}
		if n := strings.Count(errOut, "wings "+args[0]+" ls"); n > 1 {
			t.Errorf("wings %s suggests the same listing %d times: %s", strings.Join(args, " "), n, errOut)
		}
	}
	if _, _, code := run(t, "workload", "get", "chat"); code != 0 {
		t.Fatal("a prefix-addressed rm deleted the workload anyway")
	}
	if _, errOut, code := run(t, "workload", "rm", "chat", "--yes"); code != 0 {
		t.Fatalf("rm by the full name exited %d: %s", code, errOut)
	}
}

// Every rm says what it removed in every machine format.
func TestDeletesEmitAResult(t *testing.T) {
	for _, format := range []string{"json", "yaml"} {
		t.Run(format, func(t *testing.T) {
			t.Setenv("WINGSMITH_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
			for _, args := range [][]string{
				{"workload", "rm", "chat", "--yes"},
				{"serving-quota", "rm", "chat", "--yes"},
				{"discovered", "rm", "llama-70b-vllm"},
			} {
				out, errOut, code := run(t, append(args, "-o", format)...)
				if code != 0 {
					t.Fatalf("%v exit %d: %s", args, code, errOut)
				}
				if !strings.Contains(out, "chat") && !strings.Contains(out, "llama-70b-vllm") {
					t.Errorf("%v -o %s does not name what it removed: %q", args, format, out)
				}
				if !strings.Contains(out, "deleted") && !strings.Contains(out, "removed") {
					t.Errorf("%v -o %s printed no result: %q", args, format, out)
				}
			}
		})
	}
}
