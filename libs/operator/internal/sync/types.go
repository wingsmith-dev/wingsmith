// Package sync implements the outbound WebSocket client that carries the plane <-> cluster
// stream. It owns four things that are hand-written by design because we chose WebSocket+JSON
// over gRPC: message framing, request/response correlation, sequence-based resume, and
// backpressure on the send path.
//
// The envelope and every payload type live in internal/wire, which is the Go half of
// libs/packages/contract/PROTOCOL.md and libs/packages/contract/MESSAGES.md. This package holds no wire types of its own:
// a second definition of the envelope is a second contract, and only one of them would be
// tested against the fixture corpus.
package sync

import (
	"encoding/json"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// Envelope is the wire envelope. It is an alias instead of a wrapper so that a frame handed to
// a Handler is the same object the fixture corpus round-trips.
type Envelope = wire.Envelope

// MessageType is the envelope's type discriminator, `<noun>.<verb>` per MESSAGES.md.
type MessageType = string

const (
	// TypeHello is the first frame the operator sends after dialling, and is never sent twice
	// on one connection.
	TypeHello MessageType = "hello"
	// TypeWelcome is the hub's answer to TypeHello.
	TypeWelcome MessageType = "welcome"
	// TypePing is an application-level liveness probe, sent by the operator every 10s.
	TypePing MessageType = "ping"
	// TypePong answers a TypePing, correlated by ReplyTo.
	TypePong MessageType = "pong"
	// TypeAck carries an acknowledgement when there is nothing else to piggyback it on.
	TypeAck MessageType = "ack"
	// TypeError is the one generic failure frame.
	TypeError MessageType = "error"
	// TypeStreamEnd terminates a stream of frames sharing one ReplyTo. It is sequenced: a prune
	// decision depends on it, so it must not arrive out of order or go missing quietly.
	TypeStreamEnd MessageType = "stream.end"
	// TypeResyncBegin opens a full-resync stream, closed by TypeStreamEnd.
	TypeResyncBegin MessageType = "resync.begin"
)

// eventSuffix marks the frames PROTOCOL §6 exempts from replacement: an event is a point in
// time, so a newer one does not supersede an older one.
const eventSuffix = ".event"

// isEvent reports whether a frame is an event, which the send queue drops, not replaces.
func isEvent(t MessageType) bool { return strings.HasSuffix(t, eventSuffix) }

// timestampLayout is the only accepted timestamp form on the wire: RFC 3339, UTC, milliseconds.
const timestampLayout = "2006-01-02T15:04:05.000Z"

// nowTimestamp renders the current time in the wire's timestamp form.
func nowTimestamp() string { return time.Now().UTC().Format(timestampLayout) }

// newEnvelope builds a frame with the envelope fields every frame carries. Seq is assigned by the
// writer, not here, so that a frame the send queue discards never consumes a sequence number.
func newEnvelope(t MessageType, payload any) (*Envelope, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 || raw[0] != '{' {
		raw = json.RawMessage("{}")
	}
	return &Envelope{
		V:       wire.EnvelopeVersion,
		Type:    t,
		ID:      NewULID(),
		Ts:      nowTimestamp(),
		Payload: raw,
	}, nil
}

func replyTo(id string) *string { return &id }

func seqPtr(v uint64) *uint64 { return &v }
