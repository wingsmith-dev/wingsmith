package operate

import (
	"context"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
)

// tee delivers every frame to the desired-state handler and every operation to the executor.
type tee struct {
	inner    syncpkg.Handler
	executor *Executor
}

// WithExecutor returns a sync.Handler that also runs operations.
//
// Operations sit outside the resync contract entirely, which is why this composes instead of
// extending the store. A resync snapshot is the authority for pruning declared state, and an
// operation that a snapshot did not mention has not been withdrawn: it is simply not the kind of
// thing a snapshot describes. Routing it through the store would put running work within reach of
// a prune.
func WithExecutor(inner syncpkg.Handler, e *Executor) syncpkg.Handler {
	return &tee{inner: inner, executor: e}
}

// Handle implements sync.Handler.
//
// Neither an operation nor a cancellation is passed on to the inner handler. Nothing downstream has
// anything to do with either, and the store would log them as unrecognised types on every arrival.
func (t *tee) Handle(ctx context.Context, e *syncpkg.Envelope) error {
	switch e.Type {
	case TypeOperationDesired:
		return t.executor.Handle(ctx, e)
	case TypeOperationCancel:
		return t.executor.HandleCancel(ctx, e)
	}
	return t.inner.Handle(ctx, e)
}

// Resync implements sync.Handler.
//
// Nothing happens here, by design. A fresh session means the hub could not honour the resume
// point, so declared state is re-sent whole, but an operation already running in this process is
// unaffected by that, and cancelling it to await a re-send would abandon work half-done for the
// sake of a protocol event that says nothing about it.
func (t *tee) Resync(ctx context.Context) error {
	return t.inner.Resync(ctx)
}
