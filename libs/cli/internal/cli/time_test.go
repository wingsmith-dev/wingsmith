package cli

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestADurationDeadlineIsInTheFuture(t *testing.T) {
	before := time.Now()
	at, err := parseDeadline("30m", "deadline")
	if err != nil {
		t.Fatal(err)
	}
	if !at.After(before.Add(29*time.Minute)) || at.After(before.Add(31*time.Minute)) {
		t.Errorf("--deadline 30m became %s, want about 30 minutes from now", at)
	}
}

func TestASinceIsInThePast(t *testing.T) {
	before := time.Now()
	at, err := parseTime("1h", "since")
	if err != nil {
		t.Fatal(err)
	}
	if !at.Before(before.Add(-59 * time.Minute)) {
		t.Errorf("--since 1h became %s, want about an hour ago", at)
	}
}

func TestBadInstantsAreRefused(t *testing.T) {
	past := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	future := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	for _, raw := range []string{"-5m", "0s", past, "tomorrow"} {
		if _, err := parseDeadline(raw, "deadline"); err == nil {
			t.Errorf("--deadline %q was accepted", raw)
		}
	}
	if _, err := parseDeadline(future, "deadline"); err != nil {
		t.Errorf("a future RFC 3339 deadline was refused: %v", err)
	}
	if _, err := parseTime("-5m", "since"); err == nil {
		t.Error("--since -5m was accepted")
	}
	if at, err := parseDeadline("", "deadline"); at != nil || err != nil {
		t.Error("an absent deadline is not nil")
	}
}

// End to end: the operation the plane receives carries a deadline ahead of now.
func TestOperationCreateSendsAFutureDeadline(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", t.TempDir()+"/state.json")
	out, errOut, code := run(t, "operation", "create", "node.drain", "iad-1-n02", "--deadline", "2h", "--yes", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if !containsFutureDeadline(t, out) {
		t.Errorf("deadline is not in the future: %s", out)
	}
}

func containsFutureDeadline(t *testing.T, out string) bool {
	t.Helper()
	var op struct {
		DeadlineAt *time.Time `json:"deadline_at"`
	}
	if err := json.Unmarshal([]byte(out), &op); err != nil {
		t.Fatal(err)
	}
	return op.DeadlineAt != nil && op.DeadlineAt.After(time.Now().Add(time.Hour))
}

// --wait is bounded by --wait-timeout, not by the per-request --timeout: a
// short request timeout must not cut a long wait short, and a wait that runs
// out is TIMEOUT with exit 8.
func TestAWaitHasItsOwnDeadline(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", t.TempDir()+"/state.json")
	out, errOut, code := run(t, "operation", "create", "node.drain", "iad-1-n02", "--yes",
		"--wait", "--interval", "250ms", "--timeout", "100ms", "-o", "json")
	if code != 0 {
		t.Fatalf("a wait longer than --timeout was cut short: exit %d %s %s", code, out, errOut)
	}

	_, errOut, code = run(t, "benchmark", "run", "chat", "--wait", "--interval", "250ms", "--wait-timeout", "600ms", "--yes")
	if code != 8 || !strings.Contains(errOut, "TIMEOUT") {
		t.Fatalf("a wait that ran out exited %d, want 8 (TIMEOUT): %s", code, errOut)
	}
}

// Every operation is created under an idempotency key, and the one minted for
// it is reported so the caller can retry with it.
func TestOperationsAlwaysCarryAnIdempotencyKey(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", t.TempDir()+"/state.json")
	for _, args := range [][]string{
		{"operation", "create", "node.cordon", "iad-1-n01"},
		{"workload", "scale", "chat", "3"},
	} {
		out, errOut, code := run(t, append([]string{"--agent", "--yes"}, args...)...)
		if code != 0 {
			t.Fatalf("%v exit %d: %s", args, code, errOut)
		}
		var env struct {
			Meta map[string]any `json:"meta"`
		}
		if err := json.Unmarshal([]byte(out), &env); err != nil {
			t.Fatal(err)
		}
		key, _ := env.Meta["idempotency_key"].(string)
		if !strings.HasPrefix(key, "hgr-") || len(key) != len("hgr-")+32 {
			t.Errorf("%v reported idempotency key %q", args, key)
		}
	}
}
