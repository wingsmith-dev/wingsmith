package checkpoint

import (
	"context"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

// Adapter presents a Store as the sync client's Checkpointer.
//
// The two carry the same three facts (session, contiguous applied seq, and whether this side can
// still reproduce its own stream), so the adaptation is a rename instead of a translation. The
// lossy flag in particular is passed through, not folded into an empty cursor: the client
// declares it in hello, and a hub that is told the truth can hand back a fresh session instead of
// inferring one from a suspiciously blank resume.
type Adapter struct{ Store Store }

var _ syncpkg.Checkpointer = (*Adapter)(nil)

// Adapt wraps a Store as a sync.Checkpointer.
func Adapt(s Store) *Adapter { return &Adapter{Store: s} }

// Load implements sync.Checkpointer.
func (a *Adapter) Load(ctx context.Context) (syncpkg.Cursor, error) {
	st, err := a.Store.Load(ctx)
	if err != nil {
		return syncpkg.Cursor{}, err
	}
	return syncpkg.Cursor{SessionID: st.SessionID, Seq: st.AppliedSeq, Lossy: st.Lossy}, nil
}

// Save implements sync.Checkpointer. A refused advance surfaces as an error, which the client
// treats as fatal to the session: a cursor that cannot be recorded is not a cursor.
//
// A save never clears the lossy marker. Clearing is the business of the two things that actually
// repair loss (a fresh session, which the Store clears across, and a completed resync) and an
// ordinary cursor advance carries no evidence about either.
func (a *Adapter) Save(ctx context.Context, c syncpkg.Cursor) error {
	if err := a.Store.Save(ctx, State{SessionID: c.SessionID, AppliedSeq: c.Seq, Lossy: c.Lossy}); err != nil {
		return err
	}
	if c.Lossy {
		return a.Store.SetLossy(ctx, true)
	}
	return nil
}

// MarkLossy records that the local replay buffer dropped an unacknowledged frame.
func (a *Adapter) MarkLossy(ctx context.Context) error { return a.Store.SetLossy(ctx, true) }
