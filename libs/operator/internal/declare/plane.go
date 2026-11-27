// Package declare syncs the Wingsmith Workload and Endpoint resources a member cluster holds to the
// plane (ADR 0042): kubectl, Argo CD or Flux apply them here, the operator applies them there as
// field manager k8s:<cluster>/<namespace>, and reports the plane's answer and serving status in each
// resource's status.
package declare

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// Wire types this package speaks.
const (
	TypeApply   = "manifest.apply"
	TypeDelete  = "manifest.delete"
	TypeResult  = "manifest.result"
	TypeChanged = "manifest.changed"
)

// ErrUnreachable is a request the plane did not answer: no session, or no reply in time.
var ErrUnreachable = errors.New("plane unreachable")

// Plane is what the reconciler needs of the hub session.
type Plane interface {
	Apply(ctx context.Context, m *wire.ManifestApply) (*wire.ManifestResult, error)
	Delete(ctx context.Context, m *wire.ManifestDelete) (*wire.ManifestResult, error)
	// Connects counts sessions established so far; a change means a reconnect, and a relist.
	Connects() uint64
}

// Sender is the part of sync.Client a Channel sends through.
type Sender interface {
	SendCorrelated(t, subject string, payload any, replyTo string) (string, error)
	Connected() bool
	Connects() uint64
}

// Channel correlates manifest requests with the manifest.result that answers them.
type Channel struct {
	Timeout time.Duration

	mu      sync.Mutex
	sender  Sender
	pending map[string]chan *wire.ManifestResult
	changed map[string]chan *wire.ManifestChanged
}

// changedBuffer is how many manifest.changed frames per kind wait for the reconciler. A frame that
// finds it full is dropped: the periodic resync reapplies every resource anyway.
const changedBuffer = 1024

// NewChannel returns a Channel with no session bound yet; requests fail ErrUnreachable until Bind.
func NewChannel(timeout time.Duration) *Channel {
	return &Channel{
		Timeout: timeout,
		pending: map[string]chan *wire.ManifestResult{},
		changed: map[string]chan *wire.ManifestChanged{},
	}
}

// Changes is where manifest.changed frames for one kind arrive.
func (c *Channel) Changes(kind string) <-chan *wire.ManifestChanged {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.changesLocked(kind)
}

func (c *Channel) changesLocked(kind string) chan *wire.ManifestChanged {
	ch, ok := c.changed[kind]
	if !ok {
		ch = make(chan *wire.ManifestChanged, changedBuffer)
		c.changed[kind] = ch
	}
	return ch
}

// DeliverChanged queues a manifest.changed frame for its kind's reconciler.
func (c *Channel) DeliverChanged(e *syncpkg.Envelope) error {
	var m wire.ManifestChanged
	if err := json.Unmarshal(e.Payload, &m); err != nil {
		return nil //nolint:nilerr // a poison frame is dropped, never retried (PROTOCOL 6b)
	}
	c.mu.Lock()
	ch := c.changesLocked(m.Kind)
	c.mu.Unlock()
	select {
	case ch <- &m:
	default:
	}
	return nil
}

// Bind attaches the hub session. Separate from NewChannel because the session is built with the
// handler that delivers to this channel.
func (c *Channel) Bind(s Sender) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sender = s
}

func (c *Channel) Connects() uint64 {
	c.mu.Lock()
	s := c.sender
	c.mu.Unlock()
	if s == nil {
		return 0
	}
	return s.Connects()
}

func (c *Channel) Apply(ctx context.Context, m *wire.ManifestApply) (*wire.ManifestResult, error) {
	return c.call(ctx, TypeApply, m.Namespace+"/"+m.Name, m)
}

func (c *Channel) Delete(ctx context.Context, m *wire.ManifestDelete) (*wire.ManifestResult, error) {
	return c.call(ctx, TypeDelete, m.Namespace+"/"+m.Name, m)
}

func (c *Channel) call(ctx context.Context, t, subject string, payload any) (*wire.ManifestResult, error) {
	c.mu.Lock()
	s := c.sender
	c.mu.Unlock()
	// Not queued while offline: the relist on reconnect sends the current spec anyway, and a
	// queued apply would only be an older copy of it.
	if s == nil || !s.Connected() {
		return nil, ErrUnreachable
	}
	reply := make(chan *wire.ManifestResult, 1)
	// The id is assigned by SendCorrelated, so register under the lock to beat the reply.
	c.mu.Lock()
	id, err := s.SendCorrelated(t, t+":"+subject, payload, "")
	if err != nil {
		c.mu.Unlock()
		return nil, fmt.Errorf("%w: %v", ErrUnreachable, err)
	}
	c.pending[id] = reply
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}()
	timer := time.NewTimer(c.Timeout)
	defer timer.Stop()
	select {
	case r := <-reply:
		return r, nil
	case <-timer.C:
		return nil, ErrUnreachable
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// Deliver hands a manifest.result to the request waiting on it. One nobody waits on any more
// (timed out, or replayed after a resume) is dropped.
func (c *Channel) Deliver(e *syncpkg.Envelope) error {
	if e.ReplyTo == nil {
		return nil
	}
	var r wire.ManifestResult
	if err := json.Unmarshal(e.Payload, &r); err != nil {
		return nil //nolint:nilerr // a poison frame is dropped, never retried (PROTOCOL 6b)
	}
	c.mu.Lock()
	reply, ok := c.pending[*e.ReplyTo]
	c.mu.Unlock()
	if ok {
		select {
		case reply <- &r:
		default:
		}
	}
	return nil
}

// WithChannel routes manifest.result and manifest.changed frames to the channel and everything else to inner.
func WithChannel(inner syncpkg.Handler, c *Channel) syncpkg.Handler {
	return &tee{inner: inner, channel: c}
}

type tee struct {
	inner   syncpkg.Handler
	channel *Channel
}

func (t *tee) Handle(ctx context.Context, e *syncpkg.Envelope) error {
	switch e.Type {
	case TypeResult:
		return t.channel.Deliver(e)
	case TypeChanged:
		return t.channel.DeliverChanged(e)
	}
	return t.inner.Handle(ctx, e)
}

func (t *tee) Resync(ctx context.Context) error { return t.inner.Resync(ctx) }
