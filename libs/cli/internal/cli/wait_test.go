package cli

import (
	"context"
	"io"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/output"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/ui"
)

// slowOnce answers Operation with `phases` in turn, except that its first
// call outlives the per-request timeout, as a slow plane would.
type slowOnce struct {
	api.Backend
	phases []string
	calls  atomic.Int32
}

func (b *slowOnce) Operation(ctx context.Context, ref string) (api.Operation, error) {
	n := int(b.calls.Add(1))
	if n == 1 {
		<-ctx.Done()
		return api.Operation{}, errs.New(errs.CodeTimeout, "request to the plane timed out")
	}
	phase := b.phases[min(n-2, len(b.phases)-1)]
	return api.Operation{ID: ref, Type: "node.drain", Phase: phase}, nil
}

func waitApp(b api.Backend, wait time.Duration) *App {
	return &App{
		Ctx: context.Background(), Backend: b,
		Timeout: 30 * time.Millisecond, WaitTimeout: wait,
		Report: ui.NewReporter(io.Discard, ui.Style{}, false, true),
		Out:    output.New(io.Discard, io.Discard, ui.Style{}, output.FormatJSON, "", 80, false, true),
	}
}

// One poll that hits the per-request timeout is not the wait running out: the
// next poll is tried, and the wait ends when the operation does.
func TestASlowPollDoesNotEndTheWait(t *testing.T) {
	b := &slowOnce{phases: []string{api.OpRunning, api.OpSucceeded}}
	app := waitApp(b, 5*time.Second)
	if err := app.waitForOperation(&cobra.Command{Use: "wait"}, "op_1", 10*time.Millisecond); err != nil {
		t.Fatalf("wait ended on one slow poll: %v", err)
	}
	if b.calls.Load() < 3 {
		t.Errorf("polled %d times", b.calls.Load())
	}
}

// When --wait-timeout does run out, the message says so and names the phase
// the operation was last seen in.
func TestTheWaitRunningOutSaysWhereTheOperationWas(t *testing.T) {
	b := &slowOnce{phases: []string{api.OpRunning}}
	app := waitApp(b, 300*time.Millisecond)
	err := app.waitForOperation(&cobra.Command{Use: "wait"}, "op_1", 10*time.Millisecond)
	e := errs.As(err)
	if e == nil || e.Code != errs.CodeTimeout || !strings.Contains(e.Message, "was still running after 300ms") {
		t.Fatalf("err = %v", err)
	}
}

// A failure no retry gets past ends the wait at once and is reported as itself.
func TestAPermanentPollFailureEndsTheWait(t *testing.T) {
	app := waitApp(nil, 5*time.Second)
	calls := 0
	expired, err := app.poll(10*time.Millisecond, func(ctx context.Context) (bool, error) {
		calls++
		return false, errs.New(errs.CodeNotFound, "no operation op_1")
	})
	if expired || errs.As(err).Code != errs.CodeNotFound || calls != 1 {
		t.Fatalf("expired=%v err=%v calls=%d", expired, err, calls)
	}
}
