package lab

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/load"
)

func stage(concurrency, requests, succeeded int, ttftP99 time.Duration) Stage {
	return Stage{Sample: load.Sample{
		Concurrency: concurrency,
		Requests:    requests,
		Succeeded:   succeeded,
		SuccessRate: float64(succeeded) / float64(max(requests, 1)),
		TTFTP99:     ttftP99,
		TokensMin:   float64(concurrency) * 600,
	}}
}

func TestVerdictReportsTheHighestStageThatHeldTheObjective(t *testing.T) {
	report := &Report{Stages: []Stage{
		stage(4, 100, 100, 100*time.Millisecond),
		stage(16, 100, 100, 300*time.Millisecond),
		stage(64, 100, 90, 3*time.Second),
	}}
	report.Verdict(SLO{SuccessRate: 0.99, TTFTP99: time.Second})

	if report.Capacity != 16 || report.CapacityTPM != 16*600 {
		t.Fatalf("capacity %d tpm %.0f, want 16 and %d", report.Capacity, report.CapacityTPM, 16*600)
	}
	if !report.Stages[1].MetObjective || report.Stages[2].MetObjective {
		t.Fatalf("met = %v %v %v", report.Stages[0].MetObjective, report.Stages[1].MetObjective, report.Stages[2].MetObjective)
	}
	if err := report.Err(); err != nil {
		t.Fatalf("Err = %v, want nil when a stage held", err)
	}
}

func TestErrFailsARunWhereNoStageHeld(t *testing.T) {
	report := &Report{Stages: []Stage{stage(4, 100, 50, time.Second)}}
	report.Verdict(DefaultSLO())
	if err := report.Err(); !errors.Is(err, ErrObjectiveMissed) {
		t.Fatalf("Err = %v, want ErrObjectiveMissed", err)
	}
	if report.Capacity != 0 {
		t.Fatalf("capacity = %d, want 0", report.Capacity)
	}
}

func TestErrFailsARunThatMeasuredNothing(t *testing.T) {
	for _, stages := range [][]Stage{nil, {stage(4, 0, 0, 0)}} {
		report := &Report{Stages: stages}
		report.Verdict(DefaultSLO())
		if err := report.Err(); !errors.Is(err, ErrObjectiveMissed) {
			t.Fatalf("stages %v: Err = %v, want ErrObjectiveMissed", stages, err)
		}
	}
}

func TestBottleneckNamesTheEvidence(t *testing.T) {
	failed := stage(64, 100, 90, 3*time.Second)
	failed.Usage = []Usage{{Name: "gateway", CPUPeak: 95}, {Name: "agent-0", CPUPeak: 20}}
	failed.UpstreamPeak = 40
	failed.Sample.Faults = map[string]int{"http_503": 10}
	report := &Report{Stages: []Stage{stage(4, 10, 10, time.Millisecond), failed}}
	report.Verdict(DefaultSLO())

	for _, want := range []string{"gateway CPU at 95%", "never saw more than 40 of 64", "10 requests ended as http_503"} {
		if !strings.Contains(report.Bottleneck, want) {
			t.Errorf("bottleneck %q does not mention %q", report.Bottleneck, want)
		}
	}
}

func TestBottleneckSaysWhenNoLimitWasReached(t *testing.T) {
	report := &Report{Stages: []Stage{stage(4, 10, 10, time.Millisecond)}}
	report.Verdict(DefaultSLO())
	if !strings.HasPrefix(report.Bottleneck, "not reached") {
		t.Fatalf("bottleneck = %q", report.Bottleneck)
	}
}

func TestReportRendersAsTextAndJSON(t *testing.T) {
	report := &Report{Mode: "synthetic", Stages: []Stage{stage(4, 10, 10, time.Millisecond)}}
	held := 2
	report.Stages[0].Tunnels = &held
	report.Stages[0].ConfiguredTunnels = 2
	report.Stages[0].UpstreamPeak = 4
	report.Verdict(DefaultSLO())

	var text bytes.Buffer
	report.WriteText(&text)
	if !strings.Contains(text.String(), "capacity:   4 concurrent") || !strings.Contains(text.String(), "held 2 tunnels of 2 configured") {
		t.Fatalf("text report:\n%s", text.String())
	}

	var out bytes.Buffer
	if err := report.WriteJSON(&out); err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["capacity_concurrent"] != float64(4) {
		t.Fatalf("capacity_concurrent = %v", decoded["capacity_concurrent"])
	}
}

func TestFindRepoWalksUpToTheCheckout(t *testing.T) {
	root := t.TempDir()
	for _, module := range []string{"libs/gateway", "libs/agent", "libs/transit/cmd"} {
		if err := os.MkdirAll(filepath.Join(root, module), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, mod := range []string{"libs/gateway/go.mod", "libs/agent/go.mod"} {
		if err := os.WriteFile(filepath.Join(root, mod), []byte("module x\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	got, err := FindRepo(filepath.Join(root, "libs/transit/cmd"))
	if err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(root)
	if resolved, _ := filepath.EvalSymlinks(got); resolved != want {
		t.Fatalf("FindRepo = %s, want %s", got, root)
	}

	if _, err := FindRepo(t.TempDir()); err == nil {
		t.Fatal("FindRepo outside a checkout should fail")
	}
}

func TestFindRepoFindsThisCheckout(t *testing.T) {
	root, err := FindRepo(".")
	if err != nil {
		t.Skip("not running inside the monorepo")
	}
	if _, err := os.Stat(filepath.Join(root, "libs/transit/go.mod")); err != nil {
		t.Fatalf("FindRepo = %s, which does not hold libs/transit", root)
	}
}

func TestAStageWhereEveryRequestWasCutOffDidNotHoldTheObjective(t *testing.T) {
	cut := stage(4, 3, 0, time.Millisecond)
	cut.Sample.Censored = 3
	report := &Report{Stages: []Stage{cut}}
	report.Verdict(SLO{})
	if report.Stages[0].MetObjective || report.Err() == nil {
		t.Fatal("a stage with nothing settled cannot have held the objective")
	}
	if !strings.Contains(report.Bottleneck, "3 requests were still running") {
		t.Fatalf("bottleneck = %q", report.Bottleneck)
	}
}

func TestBottleneckIgnoresALegThatWasNotMeasured(t *testing.T) {
	failed := stage(64, 100, 90, 3*time.Second)
	failed.Sample.ReturnP99 = time.Millisecond
	report := &Report{Stages: []Stage{failed}}
	report.Verdict(DefaultSLO())
	if strings.Contains(report.Bottleneck, "leg") {
		t.Fatalf("bottleneck %q compares against a leg that was never stamped", report.Bottleneck)
	}
}
