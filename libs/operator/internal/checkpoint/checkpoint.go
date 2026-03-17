// Package checkpoint owns the durable resume cursor for the hub stream.
//
// PROTOCOL.md §4 requires the cursor to live beside the CRDs rather than in memory. An operator
// that restarts during a control-plane partition and comes back with an in-memory cursor rewinds
// its own stream: the hub replays from a point the operator has already passed, or worse believes
// the operator is further along than it is, and the cluster diverges from declared state with
// nothing able to detect it.
//
// Two invariants hold everywhere in this package:
//
//   - The cursor is never advanced across a gap. Within a session an update may hold the cursor
//     still or move it by exactly one; anything else is refused, because the cursor's meaning is
//     "highest contiguous applied" and a jump would assert that unapplied frames were applied.
//   - Recording is ordered after applying. A crash between the two therefore loses the record,
//     not the application: the operator resumes slightly behind and the hub replays frames that
//     were already applied. That direction is the safe one because applying desired state is
//     idempotent and re-applying converges, whereas the opposite failure — recording first —
//     would silently skip a frame and leave the cluster permanently wrong with no signal.
package checkpoint

import (
	"context"
	"errors"
	"fmt"
	stdsync "sync"
)

var (
	// ErrGap is returned when an update would advance the cursor past an unapplied frame.
	ErrGap = errors.New("checkpoint: refusing to advance cursor across a gap")

	// ErrRewind is returned when an update would move the cursor backwards within a session.
	ErrRewind = errors.New("checkpoint: refusing to rewind cursor within a session")

	// ErrNoSession is returned when an update carries no session id.
	ErrNoSession = errors.New("checkpoint: session id is required")
)

// State is the durable resume cursor.
type State struct {
	// SessionID is the hub-issued session the cursor belongs to.
	SessionID string

	// AppliedSeq is the highest contiguous inbound sequence durably applied.
	AppliedSeq uint64

	// Lossy records that the local replay buffer discarded an unacknowledged frame.
	Lossy bool
}

// Zero reports whether the state has never been written.
func (s State) Zero() bool { return s.SessionID == "" && s.AppliedSeq == 0 && !s.Lossy }

// Store persists the resume cursor.
//
// Load must distinguish "never written" (returning the zero State) from "could not be read",
// which is an error: fabricating a zero cursor after a failed read is exactly the rewind this
// package exists to prevent.
type Store interface {
	Load(ctx context.Context) (State, error)
	Save(ctx context.Context, s State) error

	// SetLossy records or clears the "local view is not a valid resume base" marker. It is set
	// when the replay buffer drops an unacknowledged frame, and also for the duration of a
	// resync epoch, so that a crash mid-resync cannot resume onto a half-rebuilt local view.
	SetLossy(ctx context.Context, lossy bool) error
}

// validateAdvance enforces the no-gap, no-rewind rule.
//
// A session change resets the cursor: a new session numbers its inbound stream from 1, so the
// only legal cursors immediately after the switch are 0 (nothing applied yet) and 1 (the first
// frame applied). Anything higher would claim to have applied frames the new session has not
// sent.
func validateAdvance(old, next State) error {
	if next.SessionID == "" {
		return ErrNoSession
	}
	if old.SessionID != next.SessionID {
		if next.AppliedSeq > 1 {
			return fmt.Errorf("%w: new session %q starts at seq 1, got %d",
				ErrGap, next.SessionID, next.AppliedSeq)
		}
		return nil
	}
	if next.AppliedSeq < old.AppliedSeq {
		return fmt.Errorf("%w: have %d, got %d", ErrRewind, old.AppliedSeq, next.AppliedSeq)
	}
	if next.AppliedSeq > old.AppliedSeq+1 {
		return fmt.Errorf("%w: have %d, got %d", ErrGap, old.AppliedSeq, next.AppliedSeq)
	}
	return nil
}

// merge carries the lossy flag forward within a session and clears it across one: a fresh session
// is precisely the repair for a lossy replay buffer, so continuing to advertise loss afterwards
// would force a resync on every subsequent reconnect.
func merge(old, next State) State {
	if old.SessionID == next.SessionID {
		next.Lossy = next.Lossy || old.Lossy
	}
	return next
}

// Memory is an in-memory Store for tests. It enforces the same invariants as the durable one so
// that a test proving gap rejection proves it about the rule, not about Kubernetes.
type Memory struct {
	mu    stdsync.Mutex
	state State
}

// Load implements Store.
func (m *Memory) Load(context.Context) (State, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.state, nil
}

// Save implements Store.
func (m *Memory) Save(_ context.Context, s State) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := validateAdvance(m.state, s); err != nil {
		return err
	}
	m.state = merge(m.state, s)
	return nil
}

// SetLossy implements Store.
func (m *Memory) SetLossy(_ context.Context, lossy bool) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.state.Lossy = lossy
	return nil
}
