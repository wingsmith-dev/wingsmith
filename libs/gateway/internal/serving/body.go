package serving

import (
	"errors"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
)

// bodyLimits is what stageReadBody enforces on one request body.
type bodyLimits struct {
	maxBytes    int64
	readTimeout time.Duration
	budget      *BodyBudget
}

// errBudgetSpent is a body that would take the gateway past its buffer budget.
var errBudgetSpent = errors.New("gateway buffer budget spent")

// BodyBudget bounds the request bytes the gateway holds at once, across every
// request on every listener sharing it.
//
// Each request body is buffered whole and kept for the life of the request,
// and the per-request cap alone does not bound how many of those are resident:
// a handful of large requests at once is an OOM rather than a refusal. With a
// budget, a body that would take the total past it is refused with 503
// no_capacity as it arrives, and its bytes are returned at once.
//
// It counts more than the bytes that arrived. A request also holds, for its
// whole life, working room equal to its body for the one stage at a time that
// decodes it, and the latest copy a rewrite made (requestState.setBody). So a
// request that is rewritten holds about three times its body against the
// budget, and the budget is close to the request-body memory actually resident.
type BodyBudget struct {
	mu    sync.Mutex
	used  int64
	limit int64
}

// NewBodyBudget returns a budget of limit bytes, or nil (no bound) for a limit
// of zero or less.
func NewBodyBudget(limit int64) *BodyBudget {
	if limit <= 0 {
		return nil
	}
	return &BodyBudget{limit: limit}
}

func (b *BodyBudget) take(n int64) bool {
	if b == nil {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.used+n > b.limit {
		return false
	}
	b.used += n
	return true
}

func (b *BodyBudget) give(n int64) {
	if b == nil || n == 0 {
		return
	}
	b.mu.Lock()
	b.used -= n
	b.mu.Unlock()
}

// Used reports the bytes currently charged.
func (b *BodyBudget) Used() int64 {
	if b == nil {
		return 0
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.used
}

// budgetReader charges every byte it reads to a budget, and fails the read
// that would overspend it.
type budgetReader struct {
	budget  *BodyBudget
	reader  io.Reader
	charged int64
	once    sync.Once
}

func (b *budgetReader) Read(p []byte) (int, error) {
	n, err := b.reader.Read(p)
	if n > 0 {
		if !b.budget.take(int64(n)) {
			return 0, errBudgetSpent
		}
		b.charged += int64(n)
	}
	return n, err
}

// release returns everything this reader charged. Safe to call more than once.
func (b *budgetReader) release() {
	b.once.Do(func() { b.budget.give(b.charged) })
}

// sameBytes reports whether two slices are the same bytes in memory, which is
// how a stage that changed nothing hands the body back.
func sameBytes(a, b []byte) bool {
	return len(a) == len(b) && (len(a) == 0 || &a[0] == &b[0])
}

// setBody replaces the request body with a stage's rewrite of it, charging the
// new copy to the buffer budget. The original stays charged by the reader that
// buffered it, because the caller's own bytes are kept for error rendering; an
// earlier rewrite is given back, because nothing holds it once replaced.
//
// It returns false, having answered 503, when the copy does not fit.
func (req *requestState) setBody(w http.ResponseWriter, r *http.Request, next []byte) bool {
	if sameBytes(next, req.body) {
		return true
	}
	n := int64(len(next))
	if !req.budget.take(n) {
		refuseOverBudget(w, r)
		return false
	}
	req.budget.give(req.copyCharge)
	req.copyCharge = n
	req.body = next
	return true
}

// refuseOverBudget answers a request whose bytes do not fit the buffer budget.
func refuseOverBudget(w http.ResponseWriter, r *http.Request) {
	MarkOutcome(r, metrics.OutcomeShed)
	writeError(w, http.StatusServiceUnavailable, "no_capacity",
		"The gateway is buffering too many requests. Retry shortly.")
}
