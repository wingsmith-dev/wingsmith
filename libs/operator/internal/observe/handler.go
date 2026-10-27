package observe

import (
	"context"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
)

// joinedHandler delegates inbound frames to the desired-state store and fans a fresh session out
// to both halves: the store opens its own resync epoch for what comes down, and the observer
// streams the whole inventory back up.
type joinedHandler struct {
	inner    syncpkg.Handler
	observer *Observer
}

// WithObserver returns a sync.Handler that also streams the observed inventory whenever the hub
// grants a fresh session. The two directions of a full resync are independent, each side opens
// and closes its own stream, so joining them here costs nothing but the ordering.
func WithObserver(inner syncpkg.Handler, o *Observer) syncpkg.Handler {
	return &joinedHandler{inner: inner, observer: o}
}

// Handle implements sync.Handler.
func (h *joinedHandler) Handle(ctx context.Context, e *syncpkg.Envelope) error {
	return h.inner.Handle(ctx, e)
}

// Resync implements sync.Handler.
func (h *joinedHandler) Resync(ctx context.Context) error {
	if err := h.inner.Resync(ctx); err != nil {
		return err
	}
	return h.observer.Resync(ctx)
}
