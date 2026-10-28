package observe

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

// fixturePayload reads the canonical example for a type. The corpus is the only thing standing in
// for codegen (PROTOCOL §8), so what this loop produces is checked against it, not against
// the Go structs, which cannot disagree with themselves.
func fixturePayload(t *testing.T, typ string) map[string]any {
	t.Helper()
	blob, err := os.ReadFile(filepath.Join("..", "wire", "fixtures", typ+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var frame struct {
		Payload map[string]any `json:"payload"`
	}
	if err := json.Unmarshal(blob, &frame); err != nil {
		t.Fatal(err)
	}
	return frame.Payload
}

func keysOf(t *testing.T, v any) map[string]any {
	t.Helper()
	blob, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(blob, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestProjectedFramesMatchTheFixtures holds the loop to the contract's field set in both
// directions: a field the fixture has and the projection omits is state the plane will never see,
// and a field the projection invents is a field no receiver is obliged to read.
func TestProjectedFramesMatchTheFixtures(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := o.Resync(t.Context()); err != nil {
		t.Fatal(err)
	}

	// The event types have no transition in this snapshot, so they are provoked separately.
	if err := o.deviceEvent("dev_x", "nod_x", "degraded", "r", "m", "2026-08-19T14:02:11.400Z"); err != nil {
		t.Fatal(err)
	}
	if err := o.nodeEvent("nod_x", "degraded", "r", "m", "2026-08-19T14:02:11.400Z"); err != nil {
		t.Fatal(err)
	}

	want := []syncpkg.MessageType{
		TypeNodeObserved, TypeNodeEvent, TypeDeviceObserved, TypeDeviceEvent,
		TypeTopologyObserved, TypeCapacityObserved,
		syncpkg.TypeResyncBegin, syncpkg.TypeStreamEnd,
	}
	for _, typ := range want {
		frames := r.ofType(typ)
		if len(frames) == 0 {
			t.Fatalf("the loop never produced a %s frame", typ)
		}
		got := keysOf(t, frames[0].payload)
		fixture := fixturePayload(t, string(typ))
		for k := range got {
			if _, ok := fixture[k]; !ok {
				t.Errorf("%s carries %q, which the fixture does not define", typ, k)
			}
		}
		var missing []string
		for k := range fixture {
			if _, ok := got[k]; !ok {
				missing = append(missing, k)
			}
		}
		sort.Strings(missing)
		for _, k := range missing {
			t.Errorf("%s omits %q, which the fixture defines", typ, k)
		}
	}
}

// TestFrameTypeNames guards the string constants against a rename that would
// leave the loop talking to nobody.
func TestFrameTypeNames(t *testing.T) {
	for typ, want := range map[syncpkg.MessageType]string{
		TypeNodeObserved:     "node.observed",
		TypeNodeEvent:        "node.event",
		TypeDeviceObserved:   "device.observed",
		TypeDeviceEvent:      "device.event",
		TypeTopologyObserved: "topology.observed",
		TypeCapacityObserved: "capacity.observed",
	} {
		if string(typ) != want {
			t.Errorf("type constant is %q, want %q", typ, want)
		}
		if _, err := os.Stat(filepath.Join("..", "wire", "fixtures", want+".json")); err != nil {
			t.Errorf("%s has no fixture, so it is not part of the contract", want)
		}
	}
}
