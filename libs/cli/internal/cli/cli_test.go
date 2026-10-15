package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
)

// run executes the CLI in-process and returns stdout, stderr, and the exit
// code. The fake is selected unless the caller sets HANGAR_FAKE=0.
func run(t *testing.T, args ...string) (string, string, int) {
	t.Helper()
	return runWith(t, filepath.Join(t.TempDir(), "config.yaml"), args...)
}

// runWith is run against a config file the caller keeps, for sequences of
// commands that read what an earlier one wrote.
func runWith(t *testing.T, configPath string, args ...string) (string, string, int) {
	t.Helper()
	t.Setenv("HANGAR_CONFIG", configPath)
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
		"artifact", "workload", "deployment", "endpoint",
		"project", "quota", "serving-quota", "priority-class", "policy",
		"allocation", "operation", "component", "event",
		"discovered", "alert-threshold",
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
// is the conflict libs/packages/contract/API.md §4 exists to settle.
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
	first, _, _ := run(t, "operation", "create", "node.drain", "iad-1-n01", "--idempotency-key", "retry-key-1", "--yes", "-o", "name")
	second, _, _ := run(t, "operation", "create", "node.drain", "iad-1-n01", "--idempotency-key", "retry-key-1", "--yes", "-o", "name")
	if strings.TrimSpace(first) == "" {
		t.Fatal("create printed no id")
	}
	if strings.TrimSpace(first) != strings.TrimSpace(second) {
		t.Errorf("the same idempotency key made two operations: %q and %q", first, second)
	}
	out, _, _ := run(t, "operation", "get", strings.TrimSpace(first), "-o", "json")
	if strings.Contains(out, "retry-key-1") {
		t.Errorf("the idempotency key leaked into the operation: %s", out)
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

// TestAdoptingAWorkloadDeclaresNothing is the guarantee the discovered noun
// exists to make. Tracking a workload records a decision; if it ever produced a
// deployment, the plane would hand the cluster a difference to reconcile and the
// model would be restarted to close it.
func TestAdoptingAWorkloadDeclaresNothing(t *testing.T) {
	before, _, code := run(t, "deployment", "ls", "-o", "name")
	if code != 0 {
		t.Fatalf("deployment ls exit %d", code)
	}

	out, _, code := run(t, "discovered", "track", "llama-70b-vllm", "--project", "core", "-o", "json")
	if code != 0 {
		t.Fatalf("track exit %d: %s", code, out)
	}
	var tracked map[string]any
	if err := json.Unmarshal([]byte(out), &tracked); err != nil {
		t.Fatalf("track is not an object: %v", err)
	}
	if tracked["status"] != "tracked" {
		t.Fatalf("status is %v, want tracked", tracked["status"])
	}
	for _, declared := range []string{"generation", "serving_digest", "desired_replicas", "revision"} {
		if _, ok := tracked[declared]; ok {
			t.Errorf("a tracked sighting carries %q, which only a declared object has", declared)
		}
	}

	after, _, code := run(t, "deployment", "ls", "-o", "name")
	if code != 0 {
		t.Fatalf("deployment ls exit %d", code)
	}
	if before != after {
		t.Errorf("tracking a discovered workload changed the declared deployments:\n%s\n%s", before, after)
	}
}

// TestIgnoringAWorkloadKeepsTheSighting: ignore is a decision about a listing,
// not a claim that the workload stopped running.
func TestIgnoringAWorkloadKeepsTheSighting(t *testing.T) {
	out, _, code := run(t, "discovered", "ignore", "sd3-batch", "-o", "json")
	if code != 0 {
		t.Fatalf("ignore exit %d: %s", code, out)
	}
	var ignored map[string]any
	if err := json.Unmarshal([]byte(out), &ignored); err != nil {
		t.Fatalf("ignore is not an object: %v", err)
	}
	if ignored["status"] != "ignored" {
		t.Fatalf("status is %v, want ignored", ignored["status"])
	}
	if ignored["observed_at"] == nil {
		t.Error("ignoring a workload dropped what the cluster observed about it")
	}
}

// TestPromotionPinsTheDigestItWasShown: the caller never types the digest. The command reads the
// workload and sends back what it was just shown, so an adoption cannot be pinned to a spec
// nobody looked at.
func TestPromotionPinsTheDigestItWasShown(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	before, _, code := run(t, "discovered", "get", "llama-70b-vllm", "-o", "json")
	if code != 0 {
		t.Fatalf("get exit %d", code)
	}
	var sighting map[string]any
	if err := json.Unmarshal([]byte(before), &sighting); err != nil {
		t.Fatalf("get is not an object: %v", err)
	}

	out, _, code := run(t, "discovered", "promote", "llama-70b-vllm",
		"--artifact", "llama-3.1-70b-instruct", "--project", "core", "-o", "json")
	if code != 0 {
		t.Fatalf("promote exit %d: %s", code, out)
	}
	var workload map[string]any
	if err := json.Unmarshal([]byte(out), &workload); err != nil {
		t.Fatalf("promote is not a workload: %v", err)
	}
	deployment := promotedDeployment(t, workload)
	if deployment["adopted_fingerprint"] != sighting["spec_fingerprint"] {
		t.Fatalf("pinned to %v but the workload reported %v",
			deployment["adopted_fingerprint"], sighting["spec_fingerprint"])
	}
	if deployment["adopted_workload_id"] != sighting["id"] {
		t.Fatal("the deployment does not point back at the sighting it was promoted from")
	}
}

// TestPromotionAdoptsTheScaleItFound: the bounds come from what is running, never from a default.
// A promotion that scaled a live workload as a side effect of cataloguing it would be the same
// class of accident as re-rendering its spec.
func TestPromotionAdoptsTheScaleItFound(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	out, _, code := run(t, "discovered", "promote", "qwen2-72b",
		"--artifact", "llama-3.1-70b-instruct", "--project", "core", "-o", "json")
	if code != 0 {
		t.Fatalf("promote exit %d: %s", code, out)
	}
	var workload map[string]any
	if err := json.Unmarshal([]byte(out), &workload); err != nil {
		t.Fatalf("promote is not a workload: %v", err)
	}
	scale, _ := workload["scale"].(map[string]any)
	if scale["desired"] != float64(2) {
		t.Errorf("the workload total is %v, but the sighting runs 2 replicas", scale["desired"])
	}
	deployment := promotedDeployment(t, workload)
	for _, field := range []string{"min_replicas", "desired_replicas", "max_replicas"} {
		if deployment[field] != float64(2) {
			t.Errorf("%s is %v, but the workload runs 2 replicas", field, deployment[field])
		}
	}
	if deployment["generation"] != float64(1) {
		t.Errorf("generation is %v, want 1 — an adoption starts where it is", deployment["generation"])
	}
}

// promotedDeployment reads back the one cluster copy a promotion made. A promotion mints the pair
// — a workload and the single deployment pinned to what is running — and the adoption fields are
// per-cluster, so they are read where they live.
func promotedDeployment(t *testing.T, workload map[string]any) map[string]any {
	t.Helper()
	deployments, _ := workload["deployments"].([]any)
	if len(deployments) != 1 {
		t.Fatalf("a promotion made %d deployments, want exactly 1", len(deployments))
	}
	share, _ := deployments[0].(map[string]any)
	out, _, code := run(t, "deployment", "get", share["id"].(string), "-o", "json")
	if code != 0 {
		t.Fatalf("deployment get exit %d: %s", code, out)
	}
	var deployment map[string]any
	if err := json.Unmarshal([]byte(out), &deployment); err != nil {
		t.Fatalf("deployment get is not an object: %v", err)
	}
	return deployment
}

// TestPromotionNeedsToKnowWhatItIsAdopting: without an artifact and an owner the plane would be
// holding a deployment it cannot describe.
func TestPromotionNeedsToKnowWhatItIsAdopting(t *testing.T) {
	_, errOut, code := run(t, "discovered", "promote", "llama-70b-vllm")
	if code == 0 {
		t.Fatal("promote succeeded with no artifact and no project")
	}
	if !strings.Contains(errOut, "--artifact") {
		t.Fatalf("the error does not say what is missing: %s", errOut)
	}
}

func TestATargetNameIsQualifiedWithTheKindTheTypeActsUpon(t *testing.T) {
	cases := []struct {
		opType string
		ref    string
		want   string
	}{
		{"node.drain", "iad-1-n01", "node:iad-1-n01"},
		{"node.cordon", "sjc-1-n03", "node:sjc-1-n03"},
		{"workload.scale", "chat", "workload:chat"},
		{"node.drain", "nod_01m0dj000000000000000000ab", "nod_01m0dj000000000000000000ab"},
		{"node.drain", "node:iad-1-n01", "node:iad-1-n01"},
		{"nonsense.type", "iad-1-n01", "iad-1-n01"},
	}
	for _, c := range cases {
		if got := qualifyOperationTarget(c.opType, c.ref); got != c.want {
			t.Errorf("%s %q became %q, wanted %q", c.opType, c.ref, got, c.want)
		}
	}
}

func TestEveryOperationTypeNamesTheKindItActsUpon(t *testing.T) {
	for _, typ := range []string{
		"node.drain", "node.cordon", "node.uncordon",
		"workload.scale", "workload.pause", "workload.reshape", "workload.drain",
	} {
		if operationTargetKinds[typ] == "" {
			t.Errorf("%s has no target kind, so a name typed for it would go bare", typ)
		}
	}
}

// A type nothing carries out is refused before a request is sent: the plane
// would record it and it would stay pending forever.
func TestOperationCreateRefusesTypesNothingCarriesOut(t *testing.T) {
	for _, typ := range []string{"node.reboot", "device.reset", "cache.warm", "deployment.roll", "component.upgrade"} {
		_, errOut, code := run(t, "operation", "create", typ, "iad-1-n01", "--yes")
		if code != 2 {
			t.Errorf("%s exited %d, want 2 (USAGE): %s", typ, code, errOut)
		}
		if !strings.Contains(errOut, "node.drain") {
			t.Errorf("%s: the error does not list the types hgr starts: %s", typ, errOut)
		}
	}
}
