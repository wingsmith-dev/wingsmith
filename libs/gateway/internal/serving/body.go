package serving

import (
	"errors"
	"io"
	"sync"
	"time"
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
// It counts bytes as they arrive, not the copies a rewrite of the body makes
// afterwards, so size the gateway's memory limit at about twice the budget.
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
