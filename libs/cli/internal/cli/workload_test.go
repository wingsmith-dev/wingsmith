package cli

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
)

// --project on workload create is the global flag, so WINGSMITH_PROJECT and the
// context's project reach it too, and nothing defaults it to a project that was
// never named.
func TestWorkloadCreateTakesTheProjectFromAnywhereItCanBeSet(t *testing.T) {
	t.Setenv("WINGSMITH_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	base := []string{"workload", "create", "assistant", "--artifact", "llama-3.1-70b-instruct",
		"--class", "h100-80", "--context-length", "8192", "-o", "json"}

	t.Setenv("WINGSMITH_PROJECT", "")
	_, errOut, code := run(t, base...)
	if code != 2 || !strings.Contains(errOut, "--project") {
		t.Fatalf("no project anywhere exited %d: %s", code, errOut)
	}

	t.Setenv("WINGSMITH_PROJECT", "core")
	out, errOut, code := run(t, base...)
	if code != 0 {
		t.Fatalf("WINGSMITH_PROJECT was not used: exit %d %s", code, errOut)
	}
	var wl map[string]any
	if err := json.Unmarshal([]byte(out), &wl); err != nil {
		t.Fatal(err)
	}
	if wl["project_id"] == "" || wl["name"] != "assistant" {
		t.Errorf("created %v", wl)
	}

	t.Setenv("WINGSMITH_PROJECT", "")
	if _, errOut, code := run(t, append([]string{"--project", "core"}, base...)...); code != 0 && !strings.Contains(errOut, "already exists") {
		t.Fatalf("--project before the verb was ignored: exit %d %s", code, errOut)
	}
}

// The configured project limits every listing that takes a project filter,
// from the flag, the environment or the context, and an explicit --project ”
// lifts it.
func TestTheConfiguredProjectLimitsListings(t *testing.T) {
	names := func(t *testing.T, args ...string) []string {
		t.Helper()
		out, errOut, code := run(t, append(args, "-o", "name")...)
		if code != 0 {
			t.Fatalf("%v: exit %d %s", args, code, errOut)
		}
		return strings.Fields(out)
	}
	for _, noun := range []string{"workload", "endpoint", "deployment", "quota", "allocation"} {
		t.Run(noun, func(t *testing.T) {
			t.Setenv("WINGSMITH_PROJECT", "")
			all := names(t, noun, "ls")
			if len(all) == 0 {
				t.Fatalf("the seed has no %s to filter", noun)
			}
			if got := names(t, noun, "ls", "--project", "core"); len(got) != len(all) {
				t.Errorf("--project core (the slug) listed %v, want %v", got, all)
			}

			t.Setenv("WINGSMITH_PROJECT", "nonexistent")
			if got := names(t, noun, "ls"); len(got) != 0 {
				t.Errorf("WINGSMITH_PROJECT=nonexistent still listed %v", got)
			}
			if got := names(t, noun, "ls", "--project", ""); len(got) != len(all) {
				t.Errorf("--project '' listed %v, want every project's %v", got, all)
			}
		})
	}
}

// Task and gang workloads are on the roadmap. create offers no flag for them,
// and the fake refuses them as the plane does.
func TestWorkloadCreateOffersOnlyServices(t *testing.T) {
	t.Setenv("WINGSMITH_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	base := []string{"workload", "create", "g1", "--project", "core", "--artifact", "llama-3.1-70b-instruct",
		"--class", "h100-80", "--context-length", "8192"}
	for _, extra := range [][]string{{"--kind", "gang"}, {"--gang-size", "2"}} {
		if _, errOut, code := run(t, append(base, extra...)...); code != 2 || !strings.Contains(errOut, "unknown flag") {
			t.Errorf("%v: exit %d %s", extra, code, errOut)
		}
	}
	out, errOut, code := run(t, append(base, "-o", "json")...)
	if code != 0 {
		t.Fatalf("a service was refused: exit %d %s", code, errOut)
	}
	var wl map[string]any
	if err := json.Unmarshal([]byte(out), &wl); err != nil {
		t.Fatal(err)
	}
	if wl["kind"] != "service" {
		t.Errorf("created kind %v", wl["kind"])
	}
}
