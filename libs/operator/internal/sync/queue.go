package sync

import (
	"context"
	"errors"
	stdsync "sync"
)

// ErrQueueFull is returned by sendQueue.Push under the disconnect policy when the queue is at
// capacity. The caller is expected to tear the connection down rather than lose an envelope.
var ErrQueueFull = errors.New("sync: send queue full")

// ErrQueueClosed is returned once the queue has been closed.
var ErrQueueClosed = errors.New("sync: send queue closed")

// sendQueue is the bounded outbound buffer between the operator's producers and the single
// WebSocket writer. It is the only backpressure point on the send path.
//
// The overflow policy is explicit rather than implicit because the three choices have genuinely
// different failure modes: drop-oldest keeps the freshest observed state at the cost of losing
// history, drop-newest preserves ordering at the cost of going stale, and disconnect trades
// availability for not silently losing anything — the stream comes back and resumes.
type sendQueue struct {
	mu      stdsync.Mutex
	notify  chan struct{}
	items   []*Envelope
	cap     int
	policy  string
	closed  bool
	dropped uint64
}

func newSendQueue(capacity int, policy string) *sendQueue {
	if capacity < 1 {
		capacity = 1
	}
	return &sendQueue{
		notify: make(chan struct{}, 1),
		items:  make([]*Envelope, 0, capacity),
		cap:    capacity,
		policy: policy,
	}
}

// Push enqueues an envelope, applying the configured overflow policy when the queue is full.
func (q *sendQueue) Push(e *Envelope) error {
	q.mu.Lock()
	if q.closed {
		q.mu.Unlock()
		return ErrQueueClosed
	}
	if len(q.items) >= q.cap {
		switch q.policy {
		case policyDisconnect:
			q.mu.Unlock()
			return ErrQueueFull
		case policyDropNewest:
			q.dropped++
			q.mu.Unlock()
			return nil
		default:
			q.items = q.items[1:]
			q.dropped++
		}
	}
	q.items = append(q.items, e)
	q.mu.Unlock()
	q.signal()
	return nil
}

// Pop blocks until an envelope is available, the queue is closed, or the context is done.
func (q *sendQueue) Pop(ctx context.Context) (*Envelope, error) {
	for {
		q.mu.Lock()
		if len(q.items) > 0 {
			e := q.items[0]
			q.items = q.items[1:]
			q.mu.Unlock()
			return e, nil
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

// Dropped reports how many envelopes have been discarded by the overflow policy.
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

// replayBuffer retains sequenced envelopes that the hub has not yet acknowledged, so that a
// reconnect can resend them. It is what makes the upward half of the stream at-least-once.
//
// The buffer is bounded by the same capacity as the send queue; overflow drops the oldest
// unacknowledged envelope and marks the buffer as lossy, which forces the next hello to be
// treated as a fresh session rather than a resume.
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

// Add records an envelope as sent but unacknowledged.
func (b *replayBuffer) Add(e *Envelope) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.items) >= b.cap {
		b.items = b.items[1:]
		b.lossy = true
	}
	b.items = append(b.items, e)
}

// Ack releases every retained envelope with a sequence number at or below seq.
func (b *replayBuffer) Ack(seq uint64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	i := 0
	for ; i < len(b.items); i++ {
		if b.items[i].Seq > seq {
			break
		}
	}
	b.items = b.items[i:]
}

// Pending returns the envelopes to replay after seq, in order.
func (b *replayBuffer) Pending(after uint64) []*Envelope {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]*Envelope, 0, len(b.items))
	for _, e := range b.items {
		if e.Seq > after {
			out = append(out, e)
		}
	}
	return out
}

// Lossy reports whether the buffer has ever discarded an unacknowledged envelope.
func (b *replayBuffer) Lossy() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.lossy
}

// Len reports how many envelopes are awaiting acknowledgement.
func (b *replayBuffer) Len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.items)
}
