package checkpoint

import (
	"context"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
)

// Adapter presents a Store through the narrower seam internal/sync currently consumes.
//
// sync.Checkpointer carries only (session, seq); it has no way to express replay-buffer loss and
// no way to report that an advance was refused as a gap rather than as a transient write failure.
// Both are preserved here — a refused advance surfaces as an error from Save, which sync already
// treats as fatal to the session — so that the durable rules hold even while the interface is
// narrower than the state.
type Adapter struct{ Store Store }

var _ syncpkg.Checkpointer = (*Adapter)(nil)

// Adapt wraps a Store as a sync.Checkpointer.
func Adapt(s Store) *Adapter { return &Adapter{Store: s} }

// Load implements sync.Checkpointer.
//
// A cursor recorded as lossy is not offered for resume: it is surfaced as an empty session id,
// which is how the operator asks the hub for a fresh session and a full resync.
func (a *Adapter) Load(ctx context.Context) (string, uint64, error) {
	st, err := a.Store.Load(ctx)
	if err != nil {
		return "", 0, err
	}
	if st.Lossy {
		return "", 0, nil
	}
	return st.SessionID, st.AppliedSeq, nil
}

// Save implements sync.Checkpointer.
func (a *Adapter) Save(ctx context.Context, session string, seq uint64) error {
	return a.Store.Save(ctx, State{SessionID: session, AppliedSeq: seq})
}

// MarkLossy records that the local replay buffer dropped an unacknowledged frame.
func (a *Adapter) MarkLossy(ctx context.Context) error { return a.Store.SetLossy(ctx, true) }
