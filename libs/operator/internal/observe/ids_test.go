package observe

import (
	"encoding/json"
	"strings"
	"testing"
)

const testCluster = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"

// assertObservedID enforces the shape MESSAGES.md makes contractual: a prefix plus a 26-character
// lowercase Crockford base32 ULID body, 30 characters in total.
func assertObservedID(t *testing.T, prefix, id string) {
	t.Helper()
	if len(id) != 30 {
		t.Fatalf("id %q is %d characters, want 30", id, len(id))
	}
	if !strings.HasPrefix(id, prefix) {
		t.Fatalf("id %q does not carry the %q prefix", id, prefix)
	}
	for _, r := range id[len(prefix):] {
		if !strings.ContainsRune(crockfordLower, r) {
			t.Fatalf("id %q contains %q, which is not lowercase Crockford base32", id, r)
		}
	}
}

const crockfordLower = "0123456789abcdefghjkmnpqrstvwxyz"

func TestObservedIDShape(t *testing.T) {
	assertObservedID(t, "nod_", NodeID(testCluster, "gpu-h100-a-07"))
	assertObservedID(t, "dev_", DeviceID(testCluster, "GPU-4c1f9a2e-7b30-4d55-9f21-0a8e6c3b1d47"))
	assertObservedID(t, "tpd_", TopologyDomainID(testCluster, "nvlink", "clique-0"))
}

// TestIDsAreStable is the property an id has to have to be an id. Deriving from the natural key
// means a restarted operator, or a second one after a leader change, re-mints byte-identical ids
// without reading anything it wrote down.
func TestIDsAreStable(t *testing.T) {
	for i := 0; i < 100; i++ {
		if got := NodeID(testCluster, "gpu-h100-a-07"); got != NodeID(testCluster, "gpu-h100-a-07") {
			t.Fatalf("node id is not a function of its key: %q", got)
		}
	}
	if NodeID(testCluster, "a") == NodeID(testCluster, "b") {
		t.Fatal("distinct node names collided")
	}
	if NodeID(testCluster, "a") == NodeID("clu_other", "a") {
		t.Fatal("one node name produced one id in two clusters")
	}
	if DeviceID(testCluster, "a") == NodeID(testCluster, "a")[4:] {
		t.Fatal("kind is not part of the derivation")
	}
	if TopologyDomainID(testCluster, "nvlink", "x") == TopologyDomainID(testCluster, "pcie", "x") {
		t.Fatal("domain kind is not part of the derivation")
	}
}

// TestNoTelemetryOnTheWire is PROTOCOL §7 asserted against the encoded frame rather than the
// struct, because the struct is not what crosses the link.
func TestNoTelemetryOnTheWire(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	forbidden := []string{
		"utilization", "utilisation", "temperature", "temp_", "power", "watt",
		"throughput", "tokens", "latency", "requests_per", "sm_clock", "fan",
	}
	for _, f := range o.Sender.(*recordingSender).frames {
		blob, err := json.Marshal(f.payload)
		if err != nil {
			t.Fatal(err)
		}
		body := strings.ToLower(string(blob))
		for _, word := range forbidden {
			if strings.Contains(body, word) {
				t.Fatalf("%s frame carries telemetry %q: %s", f.typ, word, blob)
			}
		}
		if strings.Contains(body, "memory_used") {
			t.Fatalf("%s frame reports used memory, which is a measurement: %s", f.typ, blob)
		}
	}
}
