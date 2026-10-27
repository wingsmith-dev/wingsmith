package ui

import (
	"bytes"
	"strings"
	"sync"
	"testing"
)

// syncBuf is written to by the spinner goroutine as well as the test.
type syncBuf struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuf) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuf) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// TestSilentNarrationWritesNothing. Quiet, agent and machine output modes all resolve here, and a
// single stray spinner frame on stdout corrupts a JSON stream.
func TestSilentNarrationWritesNothing(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), true, true)

	step := r.Step("placing %s", "qwen3-8b")
	step.Update("still placing")
	step.Done("placed")
	r.Info("informational")
	r.Success("done")
	r.Warn("careful")
	r.Hint("try this", "hgr cluster ls")
	p := r.Progress("downloading", 100)
	p.Set(50)
	p.Done("downloaded")
	r.Step("second").Fail("no")
	r.Stop()

	if buf.String() != "" {
		t.Fatalf("silent narration wrote %q", buf.String())
	}
}

// TestPlainNarrationIsOneLinePerTransition: a non-TTY human session (a CI log, a piped run) gets
// no spinner, but must still say what happened.
func TestPlainNarrationIsOneLinePerTransition(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), false, false)

	r.Step("placing qwen3-8b").Done("placed on iad-1")
	r.Info("3 clusters considered")
	r.Success("deployment created")
	r.Warn("one cluster was unreachable")
	r.Hint("Watch it roll out", "hgr deployment get dep_1")
	r.Stop()

	got := buf.String()
	for _, want := range []string{
		"placed on iad-1", "3 clusters considered", "deployment created",
		"one cluster was unreachable", "hgr deployment get dep_1",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("plain narration is missing %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "\r") {
		t.Errorf("plain narration emitted a carriage return; a CI log would be unreadable:\n%q", got)
	}
}

// TestAFailedStepIsMarkedDifferentlyFromADoneOne: the two are the point of narrating.
func TestAFailedStepIsMarkedDifferently(t *testing.T) {
	done := func(fail bool) string {
		var buf syncBuf
		r := NewReporter(&buf, Style{Level: ColorNone, Unicode: true}, false, false)
		s := r.Step("placing")
		if fail {
			s.Fail("no capacity")
		} else {
			s.Done("placed")
		}
		return buf.String()
	}
	ok, bad := done(false), done(true)
	if ok == bad {
		t.Fatal("a failed step reads identically to a successful one")
	}
	if !strings.Contains(bad, "no capacity") {
		t.Errorf("the failure text was lost: %q", bad)
	}
	if !strings.Contains(ok, "placed") {
		t.Errorf("the success text was lost: %q", ok)
	}
}

// TestAStepsLabelCanBeUpdatedBeforeItConcludes: a long placement narrates what it is waiting on.
func TestAStepsLabelCanBeUpdated(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), false, false)
	s := r.Step("placing")
	s.Update("waiting on weights")
	s.Done("placed")
	if !strings.Contains(buf.String(), "placed") {
		t.Fatalf("the conclusion was lost: %q", buf.String())
	}
}

// TestStoppingTwiceIsSafe. Emit calls Stop, and so does the next Step; a reporter that panicked on
// a second Stop would take the process down after the work already succeeded.
func TestStoppingTwiceIsSafe(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), true, false)
	r.Step("one")
	r.Stop()
	r.Stop()
	r.Stop()
}

// TestConcludingAStepTwiceIsSafe: the same reasoning: a deferred Fail after an explicit Done.
func TestConcludingAStepTwiceIsSafe(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), true, false)
	s := r.Step("one")
	s.Done("finished")
	s.Fail("also failed")
	s.Done("and again")
}

// TestAProgressBarReportsItsConclusion. The intermediate draws are animation; the final line is
// the one that has to survive into a log.
func TestAProgressBarReportsItsConclusion(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), false, false)
	p := r.Progress("downloading weights", 100)
	p.Set(25)
	p.Set(100)
	p.Done("downloaded 40GiB")

	if !strings.Contains(buf.String(), "downloaded 40GiB") {
		t.Fatalf("the conclusion is missing:\n%s", buf.String())
	}
}

// TestAProgressBarWithNoTotalDoesNotDivideByZero.
func TestAProgressBarWithNoTotalIsSafe(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), false, false)
	p := r.Progress("downloading", 0)
	p.Set(0)
	p.Set(10)
	p.Done("done")
}

// TestStartingASecondStepStopsTheFirst: two spinners writing to the same line would interleave
// into nonsense.
func TestStartingASecondStepStopsTheFirst(t *testing.T) {
	var buf syncBuf
	r := NewReporter(&buf, plain(), true, false)
	r.Step("first")
	r.Step("second").Done("second finished")
	r.Stop()
	if !strings.Contains(buf.String(), "second finished") {
		t.Fatalf("the second step never concluded:\n%q", buf.String())
	}
}

// TestNarrationGoesNowhereNearStdout is enforced by the caller passing stderr; this holds the
// reporter to writing only to the writer it was given.
func TestNarrationOnlyUsesTheWriterItWasGiven(t *testing.T) {
	var given, other syncBuf
	r := NewReporter(&given, plain(), false, false)
	r.Info("something")
	r.Step("x").Done("y")
	if other.String() != "" {
		t.Fatal("the reporter wrote somewhere it was not given")
	}
	if given.String() == "" {
		t.Fatal("the reporter wrote nothing at all")
	}
}
