package sync

import (
	"context"
	"errors"
	"fmt"
	stdsync "sync"
)

// ErrQueueClosed is returned once the queue has been closed.
var ErrQueueClosed = errors.New("sync: send queue closed")

// ErrQueueOverflow is returned when the queue is at its hard limit. The writer ends the session
// when it sees it, because a frame the queue could not admit is one a stream or a reply is missing.
var ErrQueueOverflow = errors.New("sync: send queue at its hard limit")

// minQueueLimit is the smallest hard limit, whatever the configured soft bound. It is sized for a
// full resync of a large cluster, whose correlated frames can neither be replaced nor dropped.
const minQueueLimit = 16384

// outbound is a queued frame together with the subject it describes. The subject is what makes
// replacement possible: two frames of the same type about the same object are two readings of one
// thing, and only the newer one is worth sending.
type outbound struct {
	env     *Envelope
	subject string
}

// sendQueue is the bounded outbound buffer between the operator's producers and the single
// WebSocket writer. It is the only backpressure point on the send path.
//
// The policy is PROTOCOL §6, operator -> hub half, and is not configurable. Below the soft bound
// every frame is appended. At or above it:
//
//   - an event is dropped from the tail and counted: a lost event cannot be re-derived, but a
//     backlog of them is not worth stalling the stream for;
//   - an uncorrelated observation replaces the queued frame with the same type and subject, since
//     the newer reading supersedes the older one;
//   - everything else is appended: stream openers and terminators, frames that belong to a
//     correlated stream, operation and benchmark reports, and an observation of a subject not
//     already queued. None of these is ever evicted to make room for another frame, because each
//     carries something no later frame restates.
//
// Above the soft bound the queue grows only by frames that cannot be replaced, so its size is
// bounded by the cluster's own inventory. The hard limit is the backstop: a Push that would pass
// it fails, and the writer ends the session.
type sendQueue struct {
	mu         stdsync.Mutex
	notify     chan struct{}
	items      []*outbound
	cap        int
	limit      int
	closed     bool
	overflowed bool
	dropped    uint64
}

func newSendQueue(capacity int) *sendQueue {
	if capacity < 1 {
		capacity = 1
	}
	return &sendQueue{
		notify: make(chan struct{}, 1),
		items:  make([]*outbound, 0, capacity),
		cap:    capacity,
		limit:  max(capacity*16, minQueueLimit),
	}
}

// Push enqueues a frame, applying the §6 policy when the queue is past its soft bound. It never
// blocks. It fails only on a closed queue or at the hard limit.
func (q *sendQueue) Push(item *outbound) error {
	q.mu.Lock()
	defer func() {
		q.mu.Unlock()
		q.signal()
	}()
	if q.closed {
		return ErrQueueClosed
	}
	if len(q.items) < q.cap {
		q.items = append(q.items, item)
		return nil
	}
	if isEvent(item.env.Type) {
		q.dropped++
		return nil
	}
	if replaceable(item) {
		if i := q.indexOf(item); i >= 0 {
			q.items[i] = item
			return nil
		}
	}
	if len(q.items) >= q.limit {
		q.overflowed = true
		return ErrQueueOverflow
	}
	q.items = append(q.items, item)
	return nil
}

// replaceable reports whether a newer frame may take a queued frame's place. Only a standalone
// reading qualifies: a frame inside a correlated stream is counted by its stream.end, and the
// stream controls and error frames each say something once.
func replaceable(item *outbound) bool {
	if item.env.ReplyTo != nil {
		return false
	}
	switch item.env.Type {
	case TypeResyncBegin, TypeStreamEnd, TypeError:
		return false
	}
	return true
}

// indexOf finds the queued frame a replaceable item supersedes.
func (q *sendQueue) indexOf(item *outbound) int {
	for i, existing := range q.items {
		if existing.env.Type == item.env.Type && existing.subject == item.subject && replaceable(existing) {
			return i
		}
	}
	return -1
}

// Pop blocks until a frame is available, the queue is closed, or the context is done.
//
// A Push refused at the hard limit is reported to the next Pop, once, so the writer ends the
// session; the frames already queued stay for the next one.
func (q *sendQueue) Pop(ctx context.Context) (*outbound, error) {
	for {
		q.mu.Lock()
		if q.overflowed {
			q.overflowed = false
			q.mu.Unlock()
			return nil, ErrQueueOverflow
		}
		if len(q.items) > 0 {
			item := q.items[0]
			q.items = q.items[1:]
			q.mu.Unlock()
			return item, nil
		}
		closed := q.closed
		q.mu.Unlock()
		if closed {
			return nil, ErrQueueClosed
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-q.notify:
		}
	}
}

// Len reports the current queue depth.
func (q *sendQueue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items)
}

// Dropped reports how many frames the overflow policy has discarded outright, which is events
// and nothing else. A replaced frame is not a drop: its subject is still represented.
func (q *sendQueue) Dropped() uint64 {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.dropped
}

// Close unblocks every waiting Pop.
func (q *sendQueue) Close() {
	q.mu.Lock()
	q.closed = true
	q.mu.Unlock()
	q.signal()
}

func (q *sendQueue) signal() {
	select {
	case q.notify <- struct{}{}:
	default:
	}
}

// errInboundOverflow ends a session whose inbound backlog has passed its bound.
var errInboundOverflow = errors.New("sync: inbound backlog past its bound; ending the session so the resume replays it")

// inboundQueue is the backlog between the reader and the applier. Push never blocks, so the
// reader is never held up by the handler; it fails instead once the backlog passes either bound.
type inboundQueue struct {
	mu        stdsync.Mutex
	notify    chan struct{}
	items     []inbound
	bytes     int
	maxFrames int
	maxBytes  int
}

func newInboundQueue(maxFrames, maxBytes int) *inboundQueue {
	return &inboundQueue{notify: make(chan struct{}, 1), maxFrames: maxFrames, maxBytes: maxBytes}
}

func (q *inboundQueue) push(in inbound) error {
	size := len(in.env.Payload)
	q.mu.Lock()
	if len(q.items) >= q.maxFrames || q.bytes+size > q.maxBytes {
		n := len(q.items)
		q.mu.Unlock()
		return fmt.Errorf("%w (%d frames waiting)", errInboundOverflow, n)
	}
	q.items = append(q.items, in)
	q.bytes += size
	q.mu.Unlock()
	select {
	case q.notify <- struct{}{}:
	default:
	}
	return nil
}

// pop blocks until a frame is waiting or the context is done.
func (q *inboundQueue) pop(ctx context.Context) (inbound, error) {
	for {
		q.mu.Lock()
		if len(q.items) > 0 {
			in := q.items[0]
			q.items[0] = inbound{}
			q.items = q.items[1:]
			q.bytes -= len(in.env.Payload)
			q.mu.Unlock()
			return in, nil
		}
		q.mu.Unlock()
		select {
		case <-ctx.Done():
			return inbound{}, ctx.Err()
		case <-q.notify:
		}
	}
}

// replayBuffer retains sequenced frames the hub has not yet acknowledged, so that a reconnect can
// resend them. It is what makes the upward half of the stream at-least-once.
//
// Overflow discards the oldest unacknowledged frame and marks the buffer lossy. Lossy is not a
// detail: it is the admission that this side can no longer reproduce its own stream, which
// PROTOCOL §4 says must be declared in the next hello rather than papered over with a resume.
type replayBuffer struct {
	mu    stdsync.Mutex
	items []*Envelope
	cap   int
	lossy bool
}

func newReplayBuffer(capacity int) *replayBuffer {
	if capacity < 1 {
		capacity = 1
	}
	return &replayBuffer{items: make([]*Envelope, 0, capacity), cap: capacity}
}

// Add records a frame as sent but unacknowledged. It reports whether the addition overflowed the
// buffer, which is the moment the stream became unresumable.
func (b *replayBuffer) Add(e *Envelope) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	overflowed := false
	if len(b.items) >= b.cap {
		b.items = b.items[1:]
		b.lossy = true
		overflowed = true
	}
	b.items = append(b.items, e)
	return overflowed
}

// Ack releases every retained frame with a sequence number at or below seq.
func (b *replayBuffer) Ack(seq uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	i := 0
	for ; i < len(b.items); i++ {
		if b.items[i].Seq == nil || *b.items[i].Seq > seq {
			break
		}
	}
	b.items = b.items[i:]
}

// Pending returns the frames to replay after seq, in order.
func (b *replayBuffer) Pending(after uint64) []*Envelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]*Envelope, 0, len(b.items))
	for _, e := range b.items {
		if e.Seq != nil && *e.Seq > after {
			out = append(out, e)
		}
	}
	return out
}

// Lossy reports whether the buffer has discarded an unacknowledged frame.
func (b *replayBuffer) Lossy() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.lossy
}

// Reset empties the buffer and clears the lossy marker. A fresh session is the repair for loss:
// nothing numbered in the old session can be replayed into the new one.
func (b *replayBuffer) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.items = b.items[:0]
	b.lossy = false
}

// Len reports how many frames are awaiting acknowledgement.
func (b *replayBuffer) Len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.items)
}
