package cli

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/hangar/cli/internal/api"
)

func TestNodeListHuman(t *testing.T) {
	out, _, code := run(t, "node", "ls", "--cluster", "iad-1", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d, want 0; output %q", code, out)
	}
	for _, want := range []string{"NAME", "CLUSTER", "POOL", "STATUS", "CORDONED", "iad-1-n01", "h100-prod", "ready"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "lab-0-n01") {
		t.Errorf("nodes of a disabled cluster listed without --all:\n%s", out)
	}
}

func TestNodeListCordonedIsObservedBoolean(t *testing.T) {
	out, _, code := run(t, "node", "ls", "--cluster", "iad-1", "--cordoned", "-o", "name")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if got := strings.TrimSpace(out); got != "iad-1-n08" {
		t.Errorf("--cordoned returned %q, want iad-1-n08", got)
	}
	all, _, _ := run(t, "node", "ls", "--cluster", "iad-1", "-o", "json")
	var got nodeList
	if err := json.Unmarshal([]byte(all), &got); err != nil {
		t.Fatal(err)
	}
	for _, n := range got.Items {
		if n.Status == "cordoned" {
			t.Fatalf("cordon leaked into the status vocabulary: %+v", n)
		}
	}
}

func TestNodeListJSONContract(t *testing.T) {
	out, _, code := run(t, "node", "ls", "--cluster", "sjc-1", "--pool", "l40s-serving", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got nodeList
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("not valid JSON: %v\n%s", err, out)
	}
	if got.Count != len(got.Items) || got.Count != 4 {
		t.Fatalf("count %d with %d items, want 4", got.Count, len(got.Items))
	}
	n := got.Items[0]
	if n.ID == "" || !strings.HasPrefix(n.ID, "nod_") || n.Cluster != "sjc-1" || n.Pool != "l40s-serving" {
		t.Errorf("node missing contract fields: %+v", n)
	}
	if len(n.Topology) == 0 || n.Topology[0].Kind == "" || !strings.HasPrefix(n.Topology[0].ID, "tpd_") {
		t.Errorf("node carries no topology domain: %+v", n.Topology)
	}
	if n.JoinedAt.IsZero() {
		t.Errorf("joined_at not carried through JSON: %+v", n)
	}
}

func TestNodeListFilters(t *testing.T) {
	out, _, code := run(t, "node", "ls", "--class", "a100-80", "--status", "not_ready", "-o", "name")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if got := strings.TrimSpace(out); got != "ams-1-n06" {
		t.Errorf("class+status filter returned %q, want ams-1-n06", got)
	}
	out, _, code = run(t, "node", "ls", "--health", "degraded", "-o", "name")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if strings.TrimSpace(out) == "" {
		t.Errorf("--health degraded matched nothing")
	}
}

func TestNodeGetByNameAndPrefix(t *testing.T) {
	var id string
	{
		out, _, _ := run(t, "node", "get", "iad-1-n01", "-o", "json")
		var n api.Node
		if err := json.Unmarshal([]byte(out), &n); err != nil {
			t.Fatal(err)
		}
		id = n.ID
	}
	for _, ref := range []string{"iad-1-n01", id, id[:10]} {
		out, _, code := run(t, "node", "get", ref, "-o", "json")
		if code != 0 {
			t.Fatalf("get %s: exit %d\n%s", ref, code, out)
		}
		var n api.Node
		if err := json.Unmarshal([]byte(out), &n); err != nil {
			t.Fatalf("get %s: bad JSON: %v", ref, err)
		}
		if n.Name != "iad-1-n01" {
			t.Errorf("get %s resolved to %q", ref, n.Name)
		}
	}
}

func TestNodeGetShowsTopologyDomains(t *testing.T) {
	out, _, code := run(t, "node", "get", "iad-1-n01", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, want := range []string{"cordoned", "conditions", "topology domains", "nvswitch", "iad-1-n01/nvswitch-0", "DEVICES"} {
		if !strings.Contains(out, want) {
			t.Errorf("node detail missing %q:\n%s", want, out)
		}
	}
}

func TestNodeGetNotFoundExitCode(t *testing.T) {
	_, errOut, code := run(t, "node", "get", "nope-9")
	if code != 4 {
		t.Fatalf("exit %d, want 4 (NOT_FOUND)", code)
	}
	if !strings.Contains(errOut, "no node named") {
		t.Errorf("stderr does not explain the failure:\n%s", errOut)
	}
}

func TestNodeListUnknownClusterIsNotFound(t *testing.T) {
	_, _, code := run(t, "node", "ls", "--cluster", "nope")
	if code != 4 {
		t.Fatalf("exit %d, want 4 (NOT_FOUND)", code)
	}
}

func TestDeviceListHuman(t *testing.T) {
	out, _, code := run(t, "device", "ls", "--node", "iad-1-n01", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d, want 0; output %q", code, out)
	}
	for _, want := range []string{"HEALTH", "STATE", "DOMAIN", "healthy", "allocated", "iad-1-n01/nvswitch-0", "8 devices"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}

func TestDeviceListJSONContract(t *testing.T) {
	out, _, code := run(t, "device", "ls", "--node", "iad-1-n01", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got deviceList
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("not valid JSON: %v\n%s", err, out)
	}
	if got.Count != 8 || len(got.Items) != 8 {
		t.Fatalf("count %d / %d items, want 8", got.Count, len(got.Items))
	}
	if got.Groups != nil {
		t.Errorf("groups present without --group-by: %+v", got.Groups)
	}
	d := got.Items[0]
	if !strings.HasPrefix(d.ID, "dev_") || !strings.HasPrefix(d.UUID, "GPU-") {
		t.Errorf("device identity is not per-device: %+v", d)
	}
	if d.Health == "" || d.MemoryGB == 0 || d.Topology == nil {
		t.Errorf("device missing health, memory, or topology: %+v", d)
	}
	if d.Allocation == nil || d.Allocation.Deployment == "" {
		t.Errorf("allocated device carries no allocation: %+v", d)
	}
	if d.AllocationStatus() != "allocated" {
		t.Errorf("allocation status = %q", d.AllocationStatus())
	}
}

func TestDeviceListFiltersHealthAndAllocation(t *testing.T) {
	out, _, code := run(t, "device", "ls", "--health", "unhealthy", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got deviceList
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatal(err)
	}
	if got.Count == 0 {
		t.Fatal("--health unhealthy matched nothing")
	}
	for _, d := range got.Items {
		if d.Health != api.HealthUnhealthy {
			t.Fatalf("health filter returned %q", d.Health)
		}
	}

	out, _, code = run(t, "device", "ls", "--cluster", "sjc-1", "--class", "h200-141", "--status", "free", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	got = deviceList{}
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatal(err)
	}
	if got.Count != 56 {
		t.Fatalf("free h200 devices = %d, want 56 (96 total - 40 allocated)", got.Count)
	}
	for _, d := range got.Items {
		if d.Allocated() {
			t.Fatalf("free filter returned an allocated device: %+v", d)
		}
	}
}

func TestDeviceGroupByDomainAnswersFabricQuestion(t *testing.T) {
	out, _, code := run(t, "device", "ls", "--cluster", "iad-1", "--status", "free", "--group-by", "domain", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got deviceList
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("not valid JSON: %v\n%s", err, out)
	}
	if got.GroupBy != "domain" || len(got.Groups) == 0 {
		t.Fatalf("grouping not reported: %+v", got.Groups)
	}
	var full int
	for _, g := range got.Groups {
		if g.Kind == "" {
			t.Errorf("group %q has no fabric kind", g.Key)
		}
		if g.Free == 8 {
			full++
		}
	}
	if full == 0 {
		t.Errorf("no domain reported eight free devices in one fabric: %+v", got.Groups)
	}
}

func TestDeviceGroupByHuman(t *testing.T) {
	out, _, code := run(t, "device", "ls", "--node", "ams-1-n08", "--group-by", "domain", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, want := range []string{"ams-1-n08/nvlink-0", "(nvlink)", "8 devices"} {
		if !strings.Contains(out, want) {
			t.Errorf("grouped output missing %q:\n%s", want, out)
		}
	}
}

func TestDeviceBadGroupByIsUsageError(t *testing.T) {
	_, _, code := run(t, "device", "ls", "--group-by", "colour")
	if code != 2 {
		t.Fatalf("exit %d, want 2 (USAGE)", code)
	}
}

func TestDeviceGetByIDUUIDAndPrefix(t *testing.T) {
	out, _, _ := run(t, "device", "ls", "--node", "iad-1-n01", "-o", "json")
	var list deviceList
	if err := json.Unmarshal([]byte(out), &list); err != nil {
		t.Fatal(err)
	}
	want := list.Items[0]
	for _, ref := range []string{want.ID, want.UUID, want.ID[:10]} {
		got, _, code := run(t, "device", "get", ref, "-o", "json")
		if code != 0 {
			t.Fatalf("get %s: exit %d\n%s", ref, code, got)
		}
		var d api.Device
		if err := json.Unmarshal([]byte(got), &d); err != nil {
			t.Fatalf("get %s: bad JSON: %v", ref, err)
		}
		if d.ID != want.ID {
			t.Errorf("get %s resolved to %s", ref, d.ID)
		}
	}
}

func TestDeviceGetHumanDetail(t *testing.T) {
	out, _, _ := run(t, "device", "ls", "--health", "degraded", "-o", "json")
	var list deviceList
	if err := json.Unmarshal([]byte(out), &list); err != nil {
		t.Fatal(err)
	}
	if list.Count == 0 {
		t.Fatal("no degraded device seeded")
	}
	got, _, code := run(t, "device", "get", list.Items[0].ID, "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, want := range []string{"health", "degraded", "ecc", "throttled", "topology", "allocation", "mig"} {
		if !strings.Contains(got, want) {
			t.Errorf("device detail missing %q:\n%s", want, got)
		}
	}
}

func TestPoolListHuman(t *testing.T) {
	out, _, code := run(t, "pool", "ls", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d, want 0; output %q", code, out)
	}
	for _, want := range []string{"NAME", "CLASS", "SOURCE", "BOUNDS", "h100-prod", "managed-metal", "16-32"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "lab-l4") {
		t.Errorf("disabled pool listed without --all:\n%s", out)
	}
}

func TestPoolListJSONContract(t *testing.T) {
	out, _, code := run(t, "pool", "ls", "--cluster", "sjc-1", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var got poolList
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("not valid JSON: %v\n%s", err, out)
	}
	if got.Count != 2 {
		t.Fatalf("count = %d, want 2", got.Count)
	}
	p := got.Items[0]
	if !strings.HasPrefix(p.ID, "pol_") || p.DeviceClass == "" || p.Source == "" {
		t.Errorf("pool missing declared spec: %+v", p)
	}
	if p.MaxNodes == 0 || p.Devices.Total == 0 {
		t.Errorf("pool missing bounds or observed membership: %+v", p)
	}
}

func TestPoolListFilters(t *testing.T) {
	out, _, code := run(t, "pool", "ls", "--class", "a100-80", "--status", "paused", "-o", "name")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	if got := strings.TrimSpace(out); got != "a100-eu" {
		t.Errorf("filters returned %q, want a100-eu", got)
	}
}

func TestPoolGetSeparatesDeclaredFromObserved(t *testing.T) {
	out, _, code := run(t, "pool", "get", "h100-prod", "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	for _, want := range []string{"declared", "observed", "device class", "node shape", "source", "nodes", "allocated"} {
		if !strings.Contains(out, want) {
			t.Errorf("pool detail missing %q:\n%s", want, out)
		}
	}
}

func TestPoolGetByPrefix(t *testing.T) {
	out, _, _ := run(t, "pool", "get", "h100-prod", "-o", "json")
	var p api.Pool
	if err := json.Unmarshal([]byte(out), &p); err != nil {
		t.Fatal(err)
	}
	got, _, code := run(t, "pool", "get", p.ID[:10], "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	var p2 api.Pool
	if err := json.Unmarshal([]byte(got), &p2); err != nil {
		t.Fatal(err)
	}
	if p2.Name != "h100-prod" {
		t.Errorf("prefix resolved to %q", p2.Name)
	}
}

func TestSupplyFaultInjection(t *testing.T) {
	for _, args := range [][]string{
		{"node", "ls"}, {"node", "get", "iad-1-n01"},
		{"device", "ls"}, {"device", "get", "dev_x"},
		{"pool", "ls"}, {"pool", "get", "h100-prod"},
	} {
		t.Setenv("HANGAR_FAKE_FAIL", "RATE_LIMITED")
		_, errOut, code := run(t, args...)
		if code != 9 {
			t.Fatalf("%v: exit %d, want 9 (RATE_LIMITED)\n%s", args, code, errOut)
		}
	}
}
