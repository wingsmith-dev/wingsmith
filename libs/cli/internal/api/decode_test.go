package api

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// The corpus in testdata is captured verbatim from a running plane. These
// tests are the reconciliation: a wire type that cannot round-trip a real
// response is a wire type that disagrees with the contract, and a field the
// client invents is worse than one it is missing, because it looks right.

func TestPagesDecodeExactly(t *testing.T) {
	checkPage[Cluster](t, "clusters.json")
	checkPage[Node](t, "nodes.json")
	checkPage[Device](t, "devices.json")
	checkPage[TopologyDomain](t, "topology-domains.json")
	checkPage[Pool](t, "pools.json")
	checkPage[Artifact](t, "artifacts.json")
	checkPage[Workload](t, "workloads.json")
	checkPage[Deployment](t, "deployments.json")
	checkPage[DiscoveredWorkload](t, "discovered.json")
	checkPage[Endpoint](t, "endpoints.json")
	checkPage[Project](t, "projects.json")
	checkPage[Quota](t, "quotas.json")
	checkPage[PriorityClass](t, "priority-classes.json")
	checkPage[Policy](t, "policies.json")
	checkPage[Allocation](t, "allocations.json")
	checkPage[Operation](t, "operations.json")
	checkPage[Component](t, "components.json")
	checkPage[Event](t, "events.json")
}

// checkPage asserts three things about one collection: the envelope is the
// envelope, every item decodes with no unknown field, and the client's own
// serialisation has exactly the server's keys — no more, no fewer.
func checkPage[T any](t *testing.T, name string) {
	t.Helper()
	raw := readFixture(t, name)

	var envelope struct {
		Items      []json.RawMessage `json:"items"`
		Count      int               `json:"count"`
		NextCursor *string           `json:"next_cursor"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatalf("%s: envelope: %v", name, err)
	}
	if envelope.Count != len(envelope.Items) {
		t.Errorf("%s: count %d but %d items", name, envelope.Count, len(envelope.Items))
	}
	if len(envelope.Items) == 0 {
		t.Fatalf("%s: fixture has no items, so it proves nothing", name)
	}

	var page Page[T]
	if err := json.Unmarshal(raw, &page); err != nil {
		t.Fatalf("%s: page: %v", name, err)
	}

	for i, item := range envelope.Items {
		var decoded T
		dec := json.NewDecoder(bytes.NewReader(item))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&decoded); err != nil {
			t.Errorf("%s[%d]: server sends a field the client does not have: %v", name, i, err)
			continue
		}
		encoded, err := json.Marshal(decoded)
		if err != nil {
			t.Fatalf("%s[%d]: re-encode: %v", name, i, err)
		}
		diffKeys(t, name, i, item, encoded)
	}
}

// diffKeys compares the key structure of two JSON objects, so a field the
// client invents is caught as loudly as one it is missing.
func diffKeys(t *testing.T, name string, index int, want, got []byte) {
	t.Helper()
	var a, b any
	if err := json.Unmarshal(want, &a); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(got, &b); err != nil {
		t.Fatal(err)
	}
	for _, problem := range compare("", a, b) {
		t.Errorf("%s[%d]: %s", name, index, problem)
	}
}

func compare(path string, want, got any) []string {
	wantMap, wantOK := want.(map[string]any)
	gotMap, gotOK := got.(map[string]any)
	if wantOK && gotOK {
		var out []string
		for _, key := range sortedKeys(wantMap) {
			if _, ok := gotMap[key]; !ok {
				out = append(out, "client is missing "+join(path, key))
				continue
			}
			out = append(out, compare(join(path, key), wantMap[key], gotMap[key])...)
		}
		for _, key := range sortedKeys(gotMap) {
			if _, ok := wantMap[key]; !ok {
				out = append(out, "client invents "+join(path, key))
			}
		}
		return out
	}

	wantList, wantIsList := want.([]any)
	gotList, gotIsList := got.([]any)
	if wantIsList && gotIsList {
		if len(wantList) != len(gotList) {
			return []string{"list length differs at " + path}
		}
		if len(wantList) == 0 {
			return nil
		}
		return compare(path+"[0]", wantList[0], gotList[0])
	}
	return nil
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func join(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return raw
}

// TestErrorShape pins the error body the CLI branches on. `detail` is an
// object: an earlier version of this client read it as a string, which meant
// every detail the plane sent was silently discarded.
func TestErrorShape(t *testing.T) {
	raw := readFixture(t, "error.json")
	var we wireError
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&we); err != nil {
		t.Fatalf("error body: %v", err)
	}
	if we.Code == "" || we.Message == "" {
		t.Fatalf("error body carried no code or message: %+v", we)
	}
	if _, ok := planeCodes[we.Code]; !ok {
		t.Errorf("plane code %q has no CLI mapping", we.Code)
	}
}

// TestQueryUsesOnlyDeclaredParams guards the one thing a filter cannot tell
// you at runtime: a parameter the server does not know is ignored in silence.
func TestQueryUsesOnlyDeclaredParams(t *testing.T) {
	c := &Client{Server: "http://example"}
	opts := ListOpts{Limit: 10, Cursor: "abc", Cluster: "iad-1", Search: "x", Labels: map[string]string{"tier": "prod"}}

	q := newQuery(opts).str("cluster", opts.Cluster).str("search", opts.Search).labels(opts.Labels).values()
	if got := q.Get("limit"); got != "10" {
		t.Errorf("limit = %q", got)
	}
	if got := q.Get("cursor"); got != "abc" {
		t.Errorf("cursor = %q", got)
	}
	if got := q["label"]; len(got) != 1 || got[0] != "tier=prod" {
		t.Errorf("label = %v, want one repeatable k=v", got)
	}
	if _, ok := q["all"]; ok {
		t.Error("`all` leaked into the query; following cursors is client-side")
	}
	_ = c
}
