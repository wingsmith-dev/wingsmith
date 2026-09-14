package cli

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestTheSLAReportNamesThePeriodTheFiguresAndTheVerdict(t *testing.T) {
	out, errOut, code := run(t, "endpoint", "sla", "chat", "--month", "2026-09", "-o", "table")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	for _, want := range []string{
		"2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "availability", "failure ratio",
		"p99", "ttft", "objective", "verdict", "met", "error budget", "availability-2026.1",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("the report does not say %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "in progress") {
		t.Error("a month that has ended was reported as still running")
	}
}

func TestTheCurrentMonthIsTheDefaultAndSaysItIsStillRunning(t *testing.T) {
	out, errOut, code := run(t, "endpoint", "sla", "chat", "-o", "table")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if !strings.Contains(out, "in progress") {
		t.Errorf("the current month is not marked as running:\n%s", out)
	}
}

func TestTheSLAReportIsMachineReadable(t *testing.T) {
	out, errOut, code := run(t, "endpoint", "sla", "chat", "--from", "2026-09-10", "--to", "2026-09-20", "-o", "json")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	var report struct {
		PeriodStart string   `json:"period_start"`
		PeriodEnd   string   `json:"period_end"`
		Met         *bool    `json:"met"`
		Consumed    *float64 `json:"error_budget_consumed"`
	}
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out)
	}
	if report.PeriodStart != "2026-09-10T00:00:00Z" || report.PeriodEnd != "2026-09-20T00:00:00Z" {
		t.Errorf("period = %s → %s", report.PeriodStart, report.PeriodEnd)
	}
	if report.Met == nil || report.Consumed == nil {
		t.Error("an endpoint with a declared objective reached no verdict")
	}
}

func TestAPeriodThePlaneWouldRefuseIsRefusedHere(t *testing.T) {
	for _, args := range [][]string{
		{"--month", "2026-09", "--from", "2026-09-01"},
		{"--to", "2026-09-01"},
		{"--from", "yesterday"},
		{"--month", "2026-13"},
	} {
		_, errOut, code := run(t, append([]string{"endpoint", "sla", "chat"}, args...)...)
		if code != 2 {
			t.Errorf("%v: exit %d, want the usage code: %s", args, code, errOut)
		}
	}
}
