package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hangar/cli/internal/api"
)

// run executes the CLI in-process and returns stdout, stderr, and the exit
// code. The fake is selected unless the caller sets HANGAR_FAKE=0.
func run(t *testing.T, args ...string) (string, string, int) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("HANGAR_CONFIG", filepath.Join(dir, "config.yaml"))
	if os.Getenv("HANGAR_FAKE") == "" {
		t.Setenv("HANGAR_FAKE", "1")
	}
	t.Setenv("NO_COLOR", "1")
	t.Setenv("COLUMNS", "200")

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
	return drain(outR), drain(errR), code
}

func drain(r *os.File) string {
	var sb strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := r.Read(buf)
		sb.Write(buf[:n])
		if err != nil {
			break
		}
	}
	return sb.String()
}

func TestEveryNounListsAndGets(t *testing.T) {
	nouns := []string{
		"cluster", "node", "device", "topology-domain", "pool",
		"artifact", "recipe", "deployment", "endpoint",
		"project", "quota", "priority-class", "policy",
		"allocation", "operation", "component", "event",
	}
	for _, n := range nouns {
		t.Run(n, func(t *testing.T) {
			out, _, code := run(t, n, "ls", "-o", "json")
			if code != 0 {
				t.Fatalf("ls exit %d: %s", code, out)
			}
			var page struct {
				Items      []map[string]any `json:"items"`
				Count      int              `json:"count"`
				NextCursor string           `json:"next_cursor"`
			}
			if err := json.Unmarshal([]byte(out), &page); err != nil {
				t.Fatalf("ls is not an envelope: %v\n%s", err, out)
			}
			if page.Count != len(page.Items) {
				t.Fatalf("count %d but %d items", page.Count, len(page.Items))
			}
			if len(page.Items) == 0 {
				t.Fatalf("%s ls returned nothing, so this proves nothing", n)
			}

			names, _, code := run(t, n, "ls", "-o", "name", "--limit", "1")
			if code != 0 {
				t.Fatalf("-o name exit %d", code)
			}
			ref := strings.TrimSpace(names)
			if ref == "" {
				t.Fatal("-o name printed nothing to identify an object by")
			}
			detail, _, code := run(t, n, "get", ref, "-o", "json")
			if code != 0 {
				t.Fatalf("get %s exit %d: %s", ref, code, detail)
			}
			var object map[string]any
			if err := json.Unmarshal([]byte(detail), &object); err != nil {
				t.Fatalf("get is not an unwrapped object: %v", err)
			}
			if _, wrapped := object["items"]; wrapped {
				t.Error("a detail response is the object itself, never an envelope")
			}
		})
	}
}

// A listing is one page, and the CLI must say so rather than presenting page
// one as the whole collection.
func TestCursorPaginationEndToEnd(t *testing.T) {
	human, stderr, code := run(t, "device", "ls", "--limit", "3", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if !strings.Contains(human, "more pages remain") {
		t.Errorf("a truncated listing read as if it were the whole collection:\n%s", human)
	}

	first, _, code := run(t, "device", "ls", "--limit", "3", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var page api.Page[api.Device]
	if err := json.Unmarshal([]byte(first), &page); err != nil {
		t.Fatal(err)
	}
	if page.Count != 3 || page.NextCursor == "" {
		t.Fatalf("want a partial page with a cursor, got count=%d cursor=%q", page.Count, page.NextCursor)
	}
	if !strings.Contains(stderr, "--cursor ") || !strings.Contains(stderr, "--all") {
		t.Errorf("a truncated listing did not offer the next page:\n%s", stderr)
	}

	second, _, code := run(t, "device", "ls", "--limit", "3", "--cursor", page.NextCursor, "-o", "json")
	if code != 0 {
		t.Fatalf("cursor exit %d", code)
	}
	var next api.Page[api.Device]
	if err := json.Unmarshal([]byte(second), &next); err != nil {
		t.Fatal(err)
	}
	if next.Items[0].ID == page.Items[0].ID {
		t.Error("the cursor returned the same page again")
	}

	everything, _, code := run(t, "device", "ls", "--all", "-o", "json")
	if code != 0 {
		t.Fatalf("--all exit %d", code)
	}
	var all api.Page[api.Device]
	if err := json.Unmarshal([]byte(everything), &all); err != nil {
		t.Fatal(err)
	}
	if all.NextCursor != "" {
		t.Error("--all stopped before the end of the collection")
	}
	if all.Count <= page.Count {
		t.Errorf("--all returned %d, no more than one page", all.Count)
	}
}

// Cordon is an observed boolean beside the condition, never a value of it.
func TestCordonIsNotACondition(t *testing.T) {
	out, _, code := run(t, "node", "ls", "--all", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var page api.Page[api.Node]
	if err := json.Unmarshal([]byte(out), &page); err != nil {
		t.Fatal(err)
	}
	cordoned := 0
	for _, n := range page.Items {
		if n.Condition == "cordoned" {
			t.Fatalf("cordon leaked into the condition vocabulary: %+v", n)
		}
		if n.Cordoned {
			cordoned++
		}
	}
	if cordoned == 0 {
		t.Skip("no cordoned node in this corpus")
	}
	only, _, code := run(t, "node", "ls", "--cordoned", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var filtered api.Page[api.Node]
	if err := json.Unmarshal([]byte(only), &filtered); err != nil {
		t.Fatal(err)
	}
	if filtered.Count != cordoned {
		t.Errorf("--cordoned returned %d of %d cordoned nodes", filtered.Count, cordoned)
	}
}

// Phase is declared and reachability is observed, and folding them together
// is the conflict contract/API.md §4 exists to settle.
func TestPhaseAndReachabilityAreSeparate(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var page api.Page[api.Cluster]
	if err := json.Unmarshal([]byte(out), &page); err != nil {
		t.Fatal(err)
	}
	for _, c := range page.Items {
		switch c.Phase {
		case api.PhasePending, api.PhaseActive, api.PhaseDraining, api.PhaseDisabled:
		default:
			t.Errorf("%s has phase %q, which is not a declared phase", c.Name, c.Phase)
		}
		if c.Sync.State == "" {
			t.Errorf("%s carries no observed sync state", c.Name)
		}
	}
}

// The imperative surface is one endpoint: a type and a target, then polling.
func TestOperationLifecycle(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	out, _, code := run(t, "operation", "create", "node.drain", "iad-1-n02", "--reason", "firmware", "--yes", "-o", "json")
	if code != 0 {
		t.Fatalf("create exit %d: %s", code, out)
	}
	var op api.Operation
	if err := json.Unmarshal([]byte(out), &op); err != nil {
		t.Fatal(err)
	}
	if op.Type != "node.drain" || op.Target.Type != "node" {
		t.Fatalf("operation did not carry its type and target kind: %+v", op)
	}
	if !strings.HasPrefix(op.Target.ID, "nod_") {
		t.Errorf("target %q was not resolved to a public id", op.Target.ID)
	}
	if op.Phase != api.OpPending {
		t.Errorf("a new operation started as %q", op.Phase)
	}

	waited, _, code := run(t, "operation", "wait", op.ID, "--interval", "250ms", "-o", "json")
	if code != 0 {
		t.Fatalf("wait exit %d: %s", code, waited)
	}
	var done api.Operation
	if err := json.Unmarshal([]byte(waited), &done); err != nil {
		t.Fatal(err)
	}
	if !done.Terminal() {
		t.Errorf("wait returned a non-terminal operation: %s", done.Phase)
	}
}

func TestOperationCancelIsNotDeletion(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	out, _, code := run(t, "operation", "create", "node.drain", "iad-1-n03", "--yes", "-o", "json")
	if code != 0 {
		t.Fatalf("create exit %d", code)
	}
	var op api.Operation
	if err := json.Unmarshal([]byte(out), &op); err != nil {
		t.Fatal(err)
	}
	cancelled, _, code := run(t, "operation", "cancel", op.ID, "--yes", "-o", "json")
	if code != 0 {
		t.Fatalf("cancel exit %d: %s", code, cancelled)
	}
	var after api.Operation
	if err := json.Unmarshal([]byte(cancelled), &after); err != nil {
		t.Fatal(err)
	}
	if after.Phase != api.OpCancelled {
		t.Errorf("cancel left the operation %q", after.Phase)
	}
	still, _, code := run(t, "operation", "get", op.ID, "-o", "json")
	if code != 0 {
		t.Fatalf("a cancelled operation was deleted rather than kept: %s", still)
	}
}

func TestIdempotencyKeyReturnsTheSameOperation(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	first, _, _ := run(t, "operation", "create", "node.drain", "iad-1-n01", "--idempotency-key", "k1", "--yes", "-o", "name")
	second, _, _ := run(t, "operation", "create", "node.drain", "iad-1-n01", "--idempotency-key", "k1", "--yes", "-o", "name")
	if strings.TrimSpace(first) == "" {
		t.Fatal("create printed no id")
	}
	if strings.TrimSpace(first) != strings.TrimSpace(second) {
		t.Errorf("the same idempotency key made two operations: %q and %q", first, second)
	}
}

func TestAmbiguousPrefixIsRefused(t *testing.T) {
	_, _, code := run(t, "node", "get", "iad-1-n0")
	if code != 5 {
		t.Errorf("an ambiguous prefix exited %d, want 5 (AMBIGUOUS)", code)
	}
	_, _, code = run(t, "cluster", "get", "iad")
	if code != 0 {
		t.Errorf("a unique prefix exited %d, want 0", code)
	}
}

func TestErrorCodesMapFromThePlane(t *testing.T) {
	_, _, code := run(t, "cluster", "get", "nothing-like-this")
	if code != 4 {
		t.Errorf("not found exited %d, want 4", code)
	}
	t.Setenv("HANGAR_FAKE_FAIL", "CONFLICT")
	_, _, code = run(t, "cluster", "ls")
	if code != 5 {
		t.Errorf("injected CONFLICT exited %d, want 5", code)
	}
}

func TestLabelSelectorIsRepeatable(t *testing.T) {
	out, _, code := run(t, "cluster", "ls", "-l", "tier=prod", "-l", "fabric=nvswitch", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, out)
	}
	var page api.Page[api.Cluster]
	if err := json.Unmarshal([]byte(out), &page); err != nil {
		t.Fatal(err)
	}
	if page.Count == 0 {
		t.Fatal("a label selector that should match matched nothing")
	}
	_, _, code = run(t, "cluster", "ls", "-l", "tier")
	if code != 2 {
		t.Errorf("a malformed label exited %d, want 2 (USAGE)", code)
	}
}

func TestAgentEnvelope(t *testing.T) {
	out, _, code := run(t, "--agent", "cluster", "ls")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var envelope struct {
		SchemaVersion string         `json:"schema_version"`
		OK            bool           `json:"ok"`
		Command       string         `json:"command"`
		Data          api.Page[any]  `json:"data"`
		Meta          map[string]any `json:"meta"`
	}
	if err := json.Unmarshal([]byte(out), &envelope); err != nil {
		t.Fatal(err)
	}
	if !envelope.OK || envelope.Command != "cluster ls" {
		t.Errorf("agent envelope: %+v", envelope)
	}
	if envelope.Meta["simulated"] != true {
		t.Error("agent mode did not mark the fake as simulated")
	}
}
