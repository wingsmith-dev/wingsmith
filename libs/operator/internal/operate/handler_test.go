package operate

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"testing"
	"time"

	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// spyHandler stands in for the desired-state store below the tee.
type spyHandler struct {
	seen     []string
	resyncs  int
	handleEr error
	resyncEr error
}

func (s *spyHandler) Handle(_ context.Context, e *syncpkg.Envelope) error {
	s.seen = append(s.seen, string(e.Type))
	return s.handleEr
}

func (s *spyHandler) Resync(_ context.Context) error {
	s.resyncs++
	return s.resyncEr
}

func teeUnder(t *testing.T) (syncpkg.Handler, *spyHandler, *recorder) {
	t.Helper()
	inner := &spyHandler{}
	rec := &recorder{}
	e := New(&Executor{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(),
		ClusterID: testCluster,
		Log:       slog.Default(),
	})
	e.Bind(rec)
	return WithExecutor(inner, e), inner, rec
}

func envelope(t *testing.T, kind string, payload any) *syncpkg.Envelope {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return &syncpkg.Envelope{Type: syncpkg.MessageType(kind), Payload: body}
}

// TestDeclaredStateReachesTheStore: everything that is not an operation passes straight through.
func TestDeclaredStateReachesTheStore(t *testing.T) {
	h, inner, _ := teeUnder(t)
	for _, kind := range []string{"deployment.desired", "artifact.desired", "component.desired"} {
		if err := h.Handle(context.Background(), envelope(t, kind, map[string]string{})); err != nil {
			t.Fatalf("handle %s: %v", kind, err)
		}
	}
	if len(inner.seen) != 3 {
		t.Fatalf("the store saw %v, want all three declared frames", inner.seen)
	}
}

// TestAnOperationDoesNotReachTheStore. Nothing downstream has anything to do with it, and the
// store would log it as an unrecognised type on every arrival.
func TestAnOperationDoesNotReachTheStore(t *testing.T) {
	h, inner, rec := teeUnder(t)
	op := operation("cordon", "gpu-0", nil)
	if err := h.Handle(context.Background(), envelope(t, TypeOperationDesired, op)); err != nil {
		t.Fatalf("handle: %v", err)
	}
	if len(inner.seen) != 0 {
		t.Errorf("the store was handed an operation: %v", inner.seen)
	}
	// It did reach the executor: the operation runs, fails to find its node, and reports.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(rec.phases()) > 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Error("the operation reached neither the store nor the executor")
}

// TestTheStoresErrorIsNotSwallowed: the tee routes, it does not decide.
func TestTheStoresErrorIsNotSwallowed(t *testing.T) {
	h, inner, _ := teeUnder(t)
	inner.handleEr = errors.New("write failed")
	if err := h.Handle(context.Background(), envelope(t, "deployment.desired", map[string]string{})); err == nil {
		t.Fatal("the tee swallowed the store's error")
	}
}

// TestResyncIsPassedDownAndLeavesOperationsAlone. A fresh session means declared state is re-sent
// whole, but an operation already running in this process is unaffected by that: cancelling it
// would abandon work half-done for the sake of a protocol event that says nothing about it.
func TestResyncIsPassedDownAndLeavesOperationsAlone(t *testing.T) {
	inner := &spyHandler{}
	e := New(&Executor{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(),
		ClusterID: testCluster,
		Log:       slog.Default(),
	})
	e.Bind(&recorder{})
	h := WithExecutor(inner, e)

	if !e.claim("opr_inflight") {
		t.Fatal("claim")
	}
	if err := h.Resync(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if inner.resyncs != 1 {
		t.Errorf("the store saw %d resyncs, want 1", inner.resyncs)
	}
	if e.claim("opr_inflight") {
		t.Error("a resync released an operation that is still running")
	}
}

func TestResyncErrorIsPropagated(t *testing.T) {
	h, inner, _ := teeUnder(t)
	inner.resyncEr = errors.New("prune failed")
	if err := h.Resync(context.Background()); err == nil {
		t.Fatal("the tee swallowed the store's resync error")
	}
}

// TestAnUndecodableOperationIsAnError instead of a silently dropped frame.
func TestAnUndecodableOperationIsAnError(t *testing.T) {
	h, _, _ := teeUnder(t)
	env := &syncpkg.Envelope{Type: TypeOperationDesired, Payload: json.RawMessage(`{"kind":`)}
	if err := h.Handle(context.Background(), env); err == nil {
		t.Fatal("a malformed operation frame was accepted")
	}
}

// TestHandleIgnoresAFrameItIsNotFor: the executor is also wired directly in places, and a
// non-operation reaching it must be a no-op instead of a decode error.
func TestHandleIgnoresAFrameItIsNotFor(t *testing.T) {
	e := New(&Executor{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(),
		ClusterID: testCluster,
		Log:       slog.Default(),
	})
	if err := e.Handle(context.Background(), envelope(t, "node.observed", map[string]string{})); err != nil {
		t.Fatalf("the executor rejected a frame that is not its business: %v", err)
	}
}

// TestReportWithNoReporterBoundDoesNotPanic. Bind happens after construction, so there is a window
// in which an operation could conclude with nothing to report to: it must survive it.
func TestReportWithNoReporterBoundDoesNotPanic(t *testing.T) {
	e := New(&Executor{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(),
		ClusterID: testCluster,
		Log:       slog.Default(),
	})
	e.report(operation("cordon", "gpu-0", nil), wire.OperationObserved{})
	e.reportBenchmark("bmr_x", wire.BenchmarkObserved{})
}
