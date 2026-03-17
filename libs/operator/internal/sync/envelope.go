// Package sync implements the outbound WebSocket client that carries the plane <-> cluster
// stream. It owns four things that are hand-written by design because we chose WebSocket+JSON
// over gRPC: message framing, request/response correlation, sequence-based resume, and
// backpressure on the send path.
package sync

import (
	"encoding/json"
	"time"
)

// MessageType discriminates envelopes on the wire.
//
// The set below is transport-level only. Every domain message travels as Payload inside a
// TypeDesired or TypeObserved envelope; the contract track owns those payload schemas.
type MessageType string

const (
	// TypeHello is the first envelope the operator sends after dialling. It carries a Hello
	// payload and establishes both directions of the resume contract.
	TypeHello MessageType = "hello"
	// TypeHelloAck is the hub's answer to TypeHello, carrying a HelloAck payload.
	TypeHelloAck MessageType = "hello.ack"
	// TypePing is an application-level liveness probe. Either side may send it.
	TypePing MessageType = "ping"
	// TypePong answers a TypePing, correlated by CorrelationID.
	TypePong MessageType = "pong"
	// TypeDesired carries declared state travelling down from the plane.
	TypeDesired MessageType = "desired"
	// TypeObserved carries observed state travelling up from the cluster.
	TypeObserved MessageType = "observed"
	// TypeAck acknowledges receipt up to Envelope.Ack, releasing the sender's replay buffer.
	TypeAck MessageType = "ack"
	// TypeError reports a protocol-level failure, optionally correlated to a request.
	TypeError MessageType = "error"
)

// Envelope is the single frame type on the wire. Every WebSocket text message is exactly one
// JSON-encoded Envelope.
type Envelope struct {
	// Type discriminates the envelope.
	Type MessageType `json:"type"`

	// ID uniquely identifies this envelope for logging and deduplication. It is stable across
	// replays: a resent envelope keeps its original ID.
	ID string `json:"id"`

	// CorrelationID is set on a reply to the ID of the envelope being answered.
	CorrelationID string `json:"correlation_id,omitempty"`

	// Seq is the sender's monotonic sequence number, starting at 1. Each direction numbers its
	// own stream independently and the counter continues across reconnects for the lifetime of
	// the operator's session with the hub. Envelopes that carry no durable state (ping, pong,
	// ack) are sent with Seq 0 and are excluded from resume.
	Seq uint64 `json:"seq"`

	// Ack is the highest contiguous Seq the sender has durably processed from the peer. It
	// piggybacks on any envelope and is the sole input to the peer's replay-buffer release.
	Ack uint64 `json:"ack,omitempty"`

	// Timestamp is when the envelope was first created, in UTC. It is not refreshed on replay.
	Timestamp time.Time `json:"ts"`

	// Payload is the type-specific body. Its schema is owned by the contract track.
	Payload json.RawMessage `json:"payload,omitempty"`
}

// Sequenced reports whether the envelope participates in resume. Liveness and acknowledgement
// frames are deliberately unsequenced so that a heartbeat can never widen a replay window.
func (e *Envelope) Sequenced() bool {
	switch e.Type {
	case TypePing, TypePong, TypeAck, TypeHello, TypeHelloAck:
		return false
	default:
		return true
	}
}

// Hello is the payload of TypeHello.
type Hello struct {
	// ClusterID is the plane-assigned cluster identity.
	ClusterID string `json:"cluster_id"`

	// Token is the static enrolment token. Auth is stubbed; see config.Config.EnrolmentToken.
	Token string `json:"token,omitempty"`

	// OperatorVersion identifies the running binary.
	OperatorVersion string `json:"operator_version"`

	// SessionID is the hub-issued session this dial is attempting to resume, empty on a first
	// connection.
	SessionID string `json:"session_id,omitempty"`

	// LastReceivedSeq is the highest contiguous hub sequence number the operator has durably
	// processed. The hub is expected to resume its stream from LastReceivedSeq+1.
	LastReceivedSeq uint64 `json:"last_received_seq"`

	// NextSendSeq is the sequence number the operator will use for its next new envelope. It
	// lets the hub detect a counter reset, which means the operator lost its buffer and the
	// hub must treat the upward stream as starting fresh.
	NextSendSeq uint64 `json:"next_send_seq"`
}

// HelloAck is the payload of TypeHelloAck.
type HelloAck struct {
	// SessionID identifies the hub-side session. A different value than the one offered in
	// Hello means the hub could not resume and the operator must resynchronise from scratch.
	SessionID string `json:"session_id"`

	// Resumed reports whether the hub honoured the requested resume point.
	Resumed bool `json:"resumed"`

	// LastReceivedSeq is the highest contiguous operator sequence the hub has durably
	// processed. The operator replays its buffer from LastReceivedSeq+1.
	LastReceivedSeq uint64 `json:"last_received_seq"`
}

// ErrorPayload is the payload of TypeError.
type ErrorPayload struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	// Fatal marks an error the operator must not retry blindly, such as a rejected token.
	Fatal bool `json:"fatal,omitempty"`
}
