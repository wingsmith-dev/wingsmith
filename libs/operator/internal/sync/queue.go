package sync

import (
	"context"
	"errors"
	stdsync "sync"
)

// ErrQueueClosed is returned once the queue has been closed.
var ErrQueueClosed = errors.New("sync: send queue closed")

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
// The policy is PROTOCOL §6, operator -> hub half, and is not configurable: observed state is a
// snapshot of the present, so a full queue replaces the oldest frame with the same type and
// subject rather than appending. Falling behind degrades resolution, never correctness. Events
// are exempt — a lost event cannot be re-derived from a later one — so they are dropped from the
// tail and the loss is counted.
type sendQueue struct {
	mu      stdsync.Mutex
	notify  chan struct{}
	items   []*outbound
	cap     int
	closed  bool
	dropped uint64
}

func newSendQueue(capacity int) *sendQueue {
	if capacity < 1 {
		capacity = 1
	}
	return &sendQueue{
		notify: make(chan struct{}, 1),
		items:  make([]*outbound, 0, capacity),
		cap:    capacity,
	}
}

// Push enqueues a frame, applying the §6 overflow policy when the queue is full. It never blocks
// and, apart from a closed queue, never fails: the policy is the answer to a slow hub.
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
	if i := q.indexOf(item.env.Type, item.subject); i >= 0 {
		q.items[i] = item
		return nil
	}
	q.items = append(q.items[1:], item)
	q.dropped++
	return nil
}

// indexOf finds the oldest queued frame describing the same subject with the same type.
func (q *sendQueue) indexOf(t MessageType, subject string) int {
	for i, existing := range q.items {
		if existing.env.Type == t && existing.subject == subject {
			return i
		}
	}
	return -1
}

// Pop blocks until a frame is available, the queue is closed, or the context is done.
func (q *sendQueue) Pop(ctx context.Context) (*outbound, error) {
	for {
		q.mu.Lock()
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
