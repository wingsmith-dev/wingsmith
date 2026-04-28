//go:build interop

package interop

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// TestHandshake is PROTOCOL §1: the token rides on the dial as a bearer header, hello is answered
// with welcome, and the session the hub named is the one the operator writes down.
func TestHandshake(t *testing.T) {
	h := startHub(t, "--desired-count", "0")
	store := newFileStore(t)
	startClient(t, h, store, newRecorder(t), nil)

	waitFor(t, "a live session", func() bool { return h.state().Session != nil })
	state := h.state()

	if len(state.AuthHeaders) == 0 || state.AuthHeaders[0] != "Bearer "+token {
		t.Fatalf("hub saw authorization %q, want %q", state.AuthHeaders, "Bearer "+token)
	}
	if state.Session.ClusterID != clusterID {
		t.Fatalf("hub bound the session to cluster %q, want %q", state.Session.ClusterID, clusterID)
	}
	welcomes := h.sentTypes("welcome")
	if len(welcomes) != 1 {
		t.Fatalf("hub sent %d welcome frames, want 1", len(welcomes))
	}
	if welcomes[0].Seq != nil {
		t.Fatalf("welcome carried seq %d; it is unsequenced", *welcomes[0].Seq)
	}
	waitFor(t, "the operator to record the session", func() bool {
		return store.mustLoad(t).SessionID == state.Session.SessionID
	})
}

// TestResumeAcrossClientRestart is the claim the whole pull architecture rests on: a second
// client process, starting from nothing but the durable cursor on disk, picks the stream up
// without losing a frame or reprocessing one.
func TestResumeAcrossClientRestart(t *testing.T) {
	h := startHub(t, "--desired-count", "0")
	store := newFileStore(t)

	first := newRecorder(t)
	clientA := startClient(t, h, store, first, nil)
	waitFor(t, "the first session", func() bool { return h.state().Session != nil })
	session := h.state().Session.SessionID

	pushed := h.push(typeDesired, 3)
	last := pushed[len(pushed)-1]
	waitFor(t, "the first client to apply everything", func() bool {
		return store.mustLoad(t).AppliedSeq == last
	})
	waitFor(t, "the hub to release its replay buffer", func() bool {
		return h.state().Session.ReplayPending == 0
	})

	clientA.stop()
	waitFor(t, "the hub to notice the client left", func() bool {
		return h.state().Session.ConnectionCount == 1
	})

	// Sent while nobody is connected: these exist only in the hub's replay buffer.
	offline := h.push(typeDesired, 2)

	second := newRecorder(t)
	startClient(t, h, store, second, nil)
	waitFor(t, "the second client to apply the backlog", func() bool {
		return store.mustLoad(t).AppliedSeq == offline[len(offline)-1]
	})

	state := h.state()
	if state.Session.SessionID != session {
		t.Fatalf("session changed across the restart: %q then %q", session, state.Session.SessionID)
	}
	if state.Session.ConnectionCount != 2 {
		t.Fatalf("session spans %d connections, want 2", state.Session.ConnectionCount)
	}
	if second.resyncCount() != 0 {
		t.Fatalf("the second client was asked to resync %d times; the resume should have held", second.resyncCount())
	}

	delivered := append(seqsOf(first.snapshot()), seqsOf(second.snapshot())...)
	seen := map[uint64]int{}
	for _, seq := range delivered {
		seen[seq]++
	}
	for seq := uint64(1); seq <= offline[len(offline)-1]; seq++ {
		if seen[seq] != 1 {
			t.Fatalf("seq %d was delivered %d times across the restart, want exactly 1 (all: %v)", seq, seen[seq], delivered)
		}
	}
}

// TestFullResync is PROTOCOL §4: an unresumable point is refused rather than pretended away, and
// each side opens its own snapshot with resync.begin and closes it with stream.end.
func TestFullResync(t *testing.T) {
	h := startHub(t, "--desired-count", "3")
	store := newFileStore(t)
	rec := newRecorder(t)
	startClient(t, h, store, rec, nil)

	waitFor(t, "the hub's resync stream to finish", func() bool {
		return len(h.sentTypes("stream.end")) == 1
	})

	begins := h.sentTypes("resync.begin")
	ends := h.sentTypes("stream.end")
	desired := h.sentTypes(typeDesired)
	if len(begins) != 1 || len(desired) != 3 {
		t.Fatalf("hub streamed %d begins and %d declarations, want 1 and 3", len(begins), len(desired))
	}
	if begins[0].Seq == nil || *begins[0].Seq != 1 {
		t.Fatalf("hub's resync.begin is seq %v, want 1 — a fresh session renumbers from 1", begins[0].Seq)
	}
	if ends[0].Seq == nil || *ends[0].Seq != 5 {
		t.Fatalf("hub's stream.end is seq %v, want 5", ends[0].Seq)
	}
	for _, f := range append(desired, ends...) {
		if f.ReplyTo == nil || *f.ReplyTo != begins[0].ID {
			t.Fatalf("%s carries reply_to %v; the stream's only correlation is the resync.begin id", f.Type, f.ReplyTo)
		}
	}

	waitFor(t, "the operator's snapshot to reach the hub", func() bool {
		return len(h.state().Received.Resyncs) == 2
	})
	state := h.state()
	begin, end := state.Received.Resyncs[0], state.Received.Resyncs[1]
	if begin.Phase != "begin" || end.Phase != "end" {
		t.Fatalf("hub recorded resync phases %q then %q", begin.Phase, end.Phase)
	}
	if begin.RequestID != end.RequestID {
		t.Fatalf("the operator's stream.end correlates to %q, its resync.begin to %q", end.RequestID, begin.RequestID)
	}
	if end.Count == nil || *end.Count != 2 || end.Complete == nil || !*end.Complete {
		t.Fatalf("operator closed its snapshot with count=%v complete=%v, want 2/true", end.Count, end.Complete)
	}
	if len(state.Received.Observed) != 2 || state.Received.Observed[0].Seq == nil || *state.Received.Observed[0].Seq != 2 {
		t.Fatalf("operator's upward stream did not restart at 1: %+v", state.Received.Observed)
	}

	// A session the hub has no record of is unresumable, and it says so rather than guessing.
	h.forget()
	h.kill()
	waitFor(t, "a second, fresh session", func() bool {
		s := h.state().Session
		return s != nil && s.SessionID != state.Session.SessionID
	})
	waitFor(t, "the operator to resync again", func() bool { return rec.resyncCount() == 2 })
	if got := h.state().Session; got.NextSeq <= 1 {
		t.Fatalf("the fresh session did not start numbering: next_seq=%d", got.NextSeq)
	}
	waitFor(t, "the operator to adopt the new session", func() bool {
		return store.mustLoad(t).SessionID == h.state().Session.SessionID
	})
}

// TestLossyForcesFreshSession is PROTOCOL §4: a sender that cannot serve as a resume base is
// given a new session rather than allowed to pretend.
func TestLossyForcesFreshSession(t *testing.T) {
	h := startHub(t, "--desired-count", "0")
	store := newFileStore(t)

	clientA := startClient(t, h, store, newRecorder(t), nil)
	waitFor(t, "the first session", func() bool { return h.state().Session != nil })
	first := h.state().Session.SessionID
	waitFor(t, "the cursor to name it", func() bool { return store.mustLoad(t).SessionID == first })
	clientA.stop()

	if err := store.SetLossy(context.Background(), true); err != nil {
		t.Fatalf("mark lossy: %v", err)
	}

	rec := newRecorder(t)
	startClient(t, h, store, rec, nil)
	waitFor(t, "a fresh session", func() bool {
		s := h.state().Session
		return s != nil && s.SessionID != first
	})
	waitFor(t, "the operator to resync", func() bool { return rec.resyncCount() == 1 })
	if state := store.mustLoad(t); state.Lossy {
		t.Fatal("the cursor is still lossy after a fresh session; a new session is the repair")
	}
}

// TestWithheldAckReplays is PROTOCOL §4: a frame the receiver cannot apply is not acknowledged
// and the sender sends it again. There is no nack, in either direction.
func TestWithheldAckReplays(t *testing.T) {
	h := startHub(t, "--desired-count", "0")
	store := newFileStore(t)
	rec := newRecorder(t)
	startClient(t, h, store, rec, nil)
	waitFor(t, "a live session", func() bool { return h.state().Session != nil })

	// Downward: the operator's handler refuses one frame.
	rec.refuseOnce(h.state().Session.NextSeq)
	refused := h.push(typeDesired, 1)[0]
	after := h.push(typeDesired, 1)[0]

	waitFor(t, "the frame after the hole to arrive", func() bool {
		for _, f := range rec.snapshot() {
			if f.Seq != nil && *f.Seq == after {
				return true
			}
		}
		return false
	})
	stayTrue(t, "the connection surviving a withheld ack", 300*time.Millisecond, func() bool {
		return h.state().Connections == 1
	})
	if got := store.mustLoad(t).AppliedSeq; got >= refused {
		t.Fatalf("cursor advanced to %d past the refused frame %d; the ack must be withheld", got, refused)
	}
	if pending := h.state().Session.ReplayPending; pending < 2 {
		t.Fatalf("hub released %d frames it was never acknowledged for", 2-pending)
	}

	h.kill()
	waitFor(t, "the refused frame to be replayed and applied", func() bool {
		return store.mustLoad(t).AppliedSeq >= after
	})
	seen := 0
	for _, f := range rec.snapshot() {
		if f.Seq != nil && *f.Seq == refused {
			seen++
		}
	}
	if seen != 1 {
		t.Fatalf("the refused frame was applied %d times, want 1 (refused once, then replayed)", seen)
	}

	// Upward: the hub's sink refuses one frame, and the operator replays it from its own buffer.
	// The baseline is whatever the operator's own opening resync already delivered.
	baseline := len(h.state().Received.Observed)
	h.failTypes(typeObserved)
	payload := fixturePayload(t, typeObserved)
	if err := rec.clientRef().Send(typeObserved, "node-x", payload); err != nil {
		t.Fatalf("send observed: %v", err)
	}
	stayTrue(t, "the hub refusing the frame", 300*time.Millisecond, func() bool {
		return len(h.state().Received.Observed) == baseline
	})
	if cursor := h.state().Session.AckCursor; cursor >= h.state().Session.ExpectedSeq-1 {
		t.Fatalf("hub acknowledged up to %d with an unapplied frame behind it", cursor)
	}
	h.failTypes()
	h.kill()
	waitFor(t, "the operator to replay the refused observation", func() bool {
		return len(h.state().Received.Observed) == baseline+1
	})
}

// TestGapClosesAndDuplicateDoesNot is PROTOCOL §4: a hole in the sequence is not accepted as one,
// and a frame already seen is dropped without ceremony.
func TestGapClosesAndDuplicateDoesNot(t *testing.T) {
	t.Run("gap", func(t *testing.T) {
		h := startHub(t, "--desired-count", "0")
		client := startClient(t, h, newFileStore(t), newRecorder(t), nil)
		waitFor(t, "a live session", func() bool { return h.state().Session != nil })

		h.pushGap(typeDesired)
		waitFor(t, "the operator to close on the gap", func() bool { return h.state().Connections > 1 })
		client.stop()
	})

	t.Run("duplicate", func(t *testing.T) {
		h := startHub(t, "--desired-count", "0")
		store := newFileStore(t)
		rec := newRecorder(t)
		startClient(t, h, store, rec, nil)
		waitFor(t, "a live session", func() bool { return h.state().Session != nil })

		seq := h.push(typeDesired, 1)[0]
		waitFor(t, "the frame to be applied", func() bool { return store.mustLoad(t).AppliedSeq == seq })

		h.duplicate()
		stayTrue(t, "the connection surviving a duplicate", 400*time.Millisecond, func() bool {
			return h.state().Connections == 1
		})
		seen := 0
		for _, f := range rec.snapshot() {
			if f.Seq != nil && *f.Seq == seq {
				seen++
			}
		}
		if seen != 1 {
			t.Fatalf("the duplicated frame reached the handler %d times, want 1", seen)
		}

		next := h.push(typeDesired, 1)[0]
		waitFor(t, "the stream to carry on", func() bool { return store.mustLoad(t).AppliedSeq == next })
	})
}

// TestLiveness is PROTOCOL §5: the application-level heartbeat holds the session open, and
// silence past the deadline is what tears it down — at both ends.
func TestLiveness(t *testing.T) {
	t.Run("heartbeat holds the session", func(t *testing.T) {
		h := startHub(t, "--desired-count", "0", "--ping-interval", "0.1", "--liveness-timeout", "1")
		client := startClient(t, h, newFileStore(t), newRecorder(t), nil)
		waitFor(t, "a live session", func() bool { return h.state().Session != nil })

		waitFor(t, "pings in both directions", func() bool {
			return len(h.sentTypes("ping")) > 0 && len(h.sentTypes("pong")) > 0
		})
		stayTrue(t, "one uninterrupted session", 1500*time.Millisecond, func() bool {
			return h.state().Connections == 1 && client.client.Connected()
		})
	})

	t.Run("operator gives up on a silent hub", func(t *testing.T) {
		h := startHub(t, "--desired-count", "0", "--ping-interval", "0.1", "--liveness-timeout", "1")
		startClient(t, h, newFileStore(t), newRecorder(t), nil)
		waitFor(t, "a live session", func() bool { return h.state().Session != nil })

		h.mute(true)
		waitFor(t, "the operator to redial after its liveness deadline", func() bool {
			return h.state().Connections > 1
		})
		h.mute(false)
		waitFor(t, "the session to come back", func() bool {
			return h.state().Session != nil && h.state().Session.ConnectionCount >= 2
		})
	})

	t.Run("hub gives up on a silent operator", func(t *testing.T) {
		h := startHub(t, "--desired-count", "0", "--ping-interval", "0.1", "--liveness-timeout", "0.5")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()

		conn := h.dialRaw(ctx)
		defer conn.CloseNow()
		payload, err := json.Marshal(wire.Hello{ClusterID: clusterID, OperatorVersion: "interop-silent"})
		if err != nil {
			t.Fatalf("encode hello: %v", err)
		}
		writeRaw(t, ctx, conn, &wire.Envelope{
			V:       wire.EnvelopeVersion,
			Type:    syncpkg.TypeHello,
			ID:      syncpkg.NewULID(),
			Ts:      "2026-08-19T14:02:11.482Z",
			Payload: payload,
		})
		if welcome := readRaw(t, ctx, conn); welcome.Type != syncpkg.TypeWelcome {
			t.Fatalf("expected welcome, got %s", welcome.Type)
		}

		// Then say nothing at all. The hub pings, gets no answer, and closes.
		closed := make(chan error, 1)
		go func() {
			for {
				if _, _, err := conn.Read(ctx); err != nil {
					closed <- err
					return
				}
			}
		}()
		select {
		case <-closed:
		case <-time.After(10 * time.Second):
			t.Fatal("hub kept a silent operator's connection open past its liveness deadline")
		}
	})
}

// TestUnknownTypeIsNotFatal is PROTOCOL §2: a type the receiver does not know is dropped and
// answered with an error, and the session carries on — which is only true if the frame still
// consumed its sequence number at both ends.
func TestUnknownTypeIsNotFatal(t *testing.T) {
	h := startHub(t, "--desired-count", "0")
	store := newFileStore(t)
	rec := newRecorder(t)
	startClient(t, h, store, rec, nil)
	waitFor(t, "a live session", func() bool { return h.state().Session != nil })

	baseline := len(h.state().Received.Observed)
	if err := rec.clientRef().Send("nonesuch.observed", "x", struct{}{}); err != nil {
		t.Fatalf("send unknown type: %v", err)
	}
	waitFor(t, "the hub to answer with an error", func() bool { return len(h.sentTypes("error")) == 1 })

	payload := fixturePayload(t, typeObserved)
	if err := rec.clientRef().Send(typeObserved, "node-y", payload); err != nil {
		t.Fatalf("send observed: %v", err)
	}
	waitFor(t, "the stream to survive the unknown type", func() bool {
		return len(h.state().Received.Observed) == baseline+1
	})
	if got := h.state().Connections; got != 1 {
		t.Fatalf("the session was re-established %d times over an unknown type; §2 says it is never fatal", got-1)
	}

	seq := h.push(typeDesired, 1)[0]
	waitFor(t, "the downward stream to survive too", func() bool {
		return store.mustLoad(t).AppliedSeq == seq
	})
}
