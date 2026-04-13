package operate

import (
	"testing"
	"time"
)

func TestParseTimeAcceptsRFC3339AndRejectsTheRest(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"", false},
		{"2026-08-23T15:04:05Z", true},
		{"2026-08-23T15:04:05+02:00", true},
		{"2026-08-23 15:04:05", false},
		{"tomorrow", false},
		{"1787158053", false},
	}
	for _, c := range cases {
		got, ok := parseTime(c.in)
		if ok != c.want {
			t.Errorf("parseTime(%q) ok = %v, want %v", c.in, ok, c.want)
		}
		if ok && got.IsZero() {
			t.Errorf("parseTime(%q) reported success with a zero time", c.in)
		}
	}
}

// TestParseTimeIsWhatArmsTheDeadline: an unparseable deadline leaves the operation unbounded
// rather than failing it immediately, which is the safer of the two for work already accepted.
func TestParseTimeRoundTripsAStamp(t *testing.T) {
	when := time.Date(2026, 8, 23, 15, 4, 5, 0, time.UTC)
	got, ok := parseTime(when.Format(time.RFC3339))
	if !ok || !got.Equal(when) {
		t.Fatalf("parseTime round trip gave %v (%v), want %v", got, ok, when)
	}
}

func TestBoolParamFallsBackOnAnythingItCannotRead(t *testing.T) {
	cases := []struct {
		params map[string]string
		def    bool
		want   bool
	}{
		{nil, true, true},
		{map[string]string{}, false, false},
		{map[string]string{"cordon": "true"}, false, true},
		{map[string]string{"cordon": "false"}, true, false},
		{map[string]string{"cordon": "1"}, false, true},
		{map[string]string{"cordon": "0"}, true, false},
		{map[string]string{"cordon": "yes"}, true, true},
		{map[string]string{"cordon": ""}, true, true},
		{map[string]string{"other": "true"}, false, false},
	}
	for _, c := range cases {
		if got := boolParam(c.params, "cordon", c.def); got != c.want {
			t.Errorf("boolParam(%v, default %v) = %v, want %v", c.params, c.def, got, c.want)
		}
	}
}

func TestIntParamFallsBackOnAnythingItCannotRead(t *testing.T) {
	cases := []struct {
		params map[string]string
		def    int
		want   int
	}{
		{nil, 0, 0},
		{map[string]string{"grace_period_seconds": "45"}, 0, 45},
		{map[string]string{"grace_period_seconds": "0"}, 30, 0},
		{map[string]string{"grace_period_seconds": "-5"}, 0, -5},
		{map[string]string{"grace_period_seconds": "soon"}, 30, 30},
		{map[string]string{"grace_period_seconds": ""}, 30, 30},
		{map[string]string{"grace_period_seconds": "45.5"}, 30, 30},
	}
	for _, c := range cases {
		if got := intParam(c.params, "grace_period_seconds", c.def); got != c.want {
			t.Errorf("intParam(%v, default %d) = %d, want %d", c.params, c.def, got, c.want)
		}
	}
}

// TestCordoningKeepsTheOperationsIdentity: the drain's first step is reported against the same
// operation, so it must not invent a new one — but it must not inherit the drain's parameters
// either, or a `cordon=false` on the drain would uncordon the node it is emptying.
func TestCordoningKeepsTheOperationsIdentity(t *testing.T) {
	drain := operation("drain", "gpu-0", map[string]string{"cordon": "false", "grace_period_seconds": "45"})
	step := cordoning(drain)

	if step.OperationID != drain.OperationID || step.ClusterID != drain.ClusterID {
		t.Error("the cordoning step lost the operation it belongs to")
	}
	if step.Target != drain.Target {
		t.Error("the cordoning step is aimed at a different node")
	}
	if step.Parameters["cordon"] != "true" {
		t.Errorf("cordon = %q; a drain must cordon regardless of what the drain's own parameters said",
			step.Parameters["cordon"])
	}
	if _, leaked := step.Parameters["grace_period_seconds"]; leaked {
		t.Error("the drain's parameters leaked into its cordoning step")
	}
	if drain.Parameters["cordon"] != "false" {
		t.Error("cordoning mutated the operation it was derived from")
	}
}

func TestStampAndAtAgreeOnFormat(t *testing.T) {
	when := time.Date(2026, 8, 23, 15, 4, 5, 123000000, time.UTC)
	if got := at(when); got != "2026-08-23T15:04:05.123Z" {
		t.Errorf("at() = %q", got)
	}
	got := stamp(when)
	if got == nil || *got != "2026-08-23T15:04:05.123Z" {
		t.Errorf("stamp() = %v", got)
	}
}

// TestStampNormalisesToUTC — the contract's timestamps are UTC, and a local-zone stamp would be
// silently wrong by hours rather than obviously malformed.
func TestStampNormalisesToUTC(t *testing.T) {
	zone := time.FixedZone("UTC+7", 7*3600)
	when := time.Date(2026, 8, 23, 22, 4, 5, 0, zone)
	got := stamp(when)
	if got == nil || *got != "2026-08-23T15:04:05.000Z" {
		t.Fatalf("stamp() = %v, want the UTC instant", got)
	}
}
