package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hangar/cli/internal/api"
)

// run executes the CLI in-process against the fake backend and returns
// stdout, stderr, and the exit code.
func run(t *testing.T, args ...string) (string, string, int) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("HANGAR_CONFIG", filepath.Join(dir, "config.yaml"))
	t.Setenv("HANGAR_STATE_DIR", dir)
	t.Setenv("HANGAR_FAKE", "1")
	t.Setenv("NO_COLOR", "1")
	t.Setenv("COLUMNS", "160")

	outR, outW, _ := os.Pipe()
	errR, errW, _ := os.Pipe()
	oldOut, oldErr, oldArgs := os.Stdout, os.Stderr, os.Args
	os.Stdout, os.Stderr = outW, errW
	os.Args = append([]string{"hgr"}, args...)
	defer func() {
		os.Stdout, os.Stderr, os.Args = oldOut, oldErr, oldArgs
	}()

	code := Execute()
	outW.Close()
	errW.Close()

	var sb, eb strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := outR.Read(buf)
		sb.Write(buf[:n])
		if err != nil {
			break
		}
	}
	for {
		n, err := errR.Read(buf)
		eb.Write(buf[:n])
		if err != nil {
			break
		}
	}
	return sb.String(), eb.String(), code
}

func TestClusterListHuman(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d, want 0; output %q", code, out)
	}
	for _, want := range []string{"NAME", "REGION", "PHASE", "iad-1", "us-east", "active"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "lab-0") {
		t.Errorf("disabled cluster listed without --all:\n%s", out)
	}
}

func TestClusterListAllIncludesDisabled(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "--all", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if !strings.Contains(out, "lab-0") {
		t.Errorf("--all did not include the disabled cluster:\n%s", out)
	}
}

func TestClusterListJSONContract(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got struct {
		Items []api.Cluster `json:"items"`
		Count int           `json:"count"`
	}
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("output is not valid JSON: %v\n%s", err, out)
	}
	if got.Count != len(got.Items) || got.Count == 0 {
		t.Fatalf("count %d does not match %d items", got.Count, len(got.Items))
	}
	first := got.Items[0]
	if first.Name == "" || first.ID == "" || first.Region == "" || first.Phase == "" {
		t.Errorf("cluster is missing contract fields: %+v", first)
	}
	if first.EnrolledAt.IsZero() {
		t.Errorf("enrolled_at not carried through JSON: %+v", first)
	}
}

func TestClusterListFilters(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "--region", "eu-west", "-o", "name")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if got := strings.TrimSpace(out); got != "ams-1" {
		t.Errorf("region filter returned %q, want ams-1", got)
	}
}

func TestClusterGetByNameAndPrefix(t *testing.T) {
	for _, ref := range []string{"iad-1", "clu_01hq0v8p2m", "clu_01hq0v8"} {
		out, _, code := run(t, "cluster", "get", ref, "-o", "json")
		if code != 0 {
			t.Fatalf("get %s: exit %d\n%s", ref, code, out)
		}
		var c api.Cluster
		if err := json.Unmarshal([]byte(out), &c); err != nil {
			t.Fatalf("get %s: bad JSON: %v", ref, err)
		}
		if c.Name != "iad-1" {
			t.Errorf("get %s resolved to %q", ref, c.Name)
		}
		if len(c.Devices) == 0 {
			t.Errorf("get %s returned no device rollup", ref)
		}
	}
}

func TestClusterGetHumanDetail(t *testing.T) {
	out, _, code := run(t, "cluster", "get", "sjc-1", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, want := range []string{"sjc-1", "us-west", "h200-141", "l40s-48", "ALLOCATED"} {
		if !strings.Contains(out, want) {
			t.Errorf("detail missing %q:\n%s", want, out)
		}
	}
}

func TestClusterGetNotFoundExitCode(t *testing.T) {
	_, errOut, code := run(t, "cluster", "get", "nope")
	if code != 4 {
		t.Fatalf("exit %d, want 4 (NOT_FOUND)", code)
	}
	if !strings.Contains(errOut, "NOT_FOUND") && !strings.Contains(errOut, "no cluster named") {
		t.Errorf("stderr does not explain the failure:\n%s", errOut)
	}
}

func TestUnimplementedNounExits5(t *testing.T) {
	_, errOut, code := run(t, "artifact", "ls")
	if code != 5 {
		t.Fatalf("exit %d, want 5 (UNIMPLEMENTED)\n%s", code, errOut)
	}
}

func TestAgentEnvelope(t *testing.T) {
	t.Setenv("HANGAR_AGENT", "1")
	out, _, code := run(t, "cluster", "ls")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var env struct {
		SchemaVersion string `json:"schema_version"`
		OK            bool   `json:"ok"`
		Command       string `json:"command"`
		Meta          struct {
			Simulated bool `json:"simulated"`
		} `json:"meta"`
	}
	if err := json.Unmarshal([]byte(out), &env); err != nil {
		t.Fatalf("envelope is not JSON: %v\n%s", err, out)
	}
	if env.SchemaVersion != "1" || !env.OK || env.Command != "cluster ls" || !env.Meta.Simulated {
		t.Errorf("unexpected envelope: %+v", env)
	}
}

func TestUnknownOutputFormatIsUsageError(t *testing.T) {
	_, _, code := run(t, "cluster", "ls", "-o", "toml")
	if code != 2 {
		t.Fatalf("exit %d, want 2 (USAGE)", code)
	}
}
