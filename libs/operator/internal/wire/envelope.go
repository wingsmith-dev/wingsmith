// Package wire holds the hand-written Go types for the plane <-> operator protocol.
//
// The envelope and its semantics are specified in libs/packages/contract/PROTOCOL.md; the type registry is
// libs/packages/contract/MESSAGES.md. These structs are hand-written by design (see ADR 0001) and are kept
// accurate against the Python side by the golden corpus in libs/packages/contract/fixtures/.
//
// Timestamps are RFC 3339 UTC with milliseconds and are carried as string, not time.Time. The
// wire form is the contract, and a time.Time round-trip is lossy with respect to its own text:
// Go re-encodes to nanosecond precision and msgspec to microsecond, so neither reproduces the
// canonical document it decoded. TimestampPattern is the validation seam.
package wire

import (
	"encoding/json"
	"fmt"
	"regexp"
)

// EnvelopeVersion is the envelope version this side sends. It is bumped only for changes to the
// envelope itself, never for payloads.
const EnvelopeVersion = 1

// MinEnvelopeVersion is the oldest envelope version this side still reads. PROTOCOL §9 gives a
// version change a period in which both are spoken, and a receiver that accepted exactly one
// version would turn that period into an outage.
const MinEnvelopeVersion = 1

// SupportedEnvelope reports whether a received envelope version is one this side reads.
func SupportedEnvelope(v int) bool {
	return v >= MinEnvelopeVersion && v <= EnvelopeVersion
}

// TimestampPattern matches the only accepted timestamp form: RFC 3339, UTC, milliseconds.
var TimestampPattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$`)

// IDPrefixes are the entity-id prefixes carried in payloads. Every prefixed id is the prefix
// followed by a 26-character ULID.
var IDPrefixes = []string{
	"clu_", "nod_", "dev_", "tpd_", "pol_", "art_", "rec_",
	"dep_", "rep_", "end_", "prj_", "qta_", "opr_", "cmp_",
}

// unsequencedTypes never carry a seq and never enter a replay buffer. See PROTOCOL §4.
var unsequencedTypes = map[string]struct{}{
	"hello":   {},
	"welcome": {},
	"ping":    {},
	"pong":    {},
	"ack":     {},
}

// Envelope is the single frame type on the wire. Every WebSocket text message is exactly one
// JSON-encoded Envelope.
type Envelope struct {
	V       int             `json:"v"`
	Type    string          `json:"type"`
	ID      string          `json:"id"`
	Seq     *uint64         `json:"seq,omitempty"`
	Ack     *uint64         `json:"ack,omitempty"`
	ReplyTo *string         `json:"reply_to,omitempty"`
	Ts      string          `json:"ts"`
	Payload json.RawMessage `json:"payload"`
}

// Sequenced reports whether the frame participates in resume.
func (e *Envelope) Sequenced() bool {
	_, ok := unsequencedTypes[e.Type]
	return !ok
}

// Frame is an Envelope whose payload has been decoded into its registered type.
type Frame struct {
	Envelope
	Message any
}

// Decode parses a frame and decodes its payload into the type registered for its Type.
//
// An unregistered type is an error the caller logs and drops, not treats as fatal, per
// PROTOCOL §2.
func Decode(data []byte) (*Frame, error) {
	var env Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return nil, err
	}
	build, ok := MessageTypes[env.Type]
	if !ok {
		return nil, fmt.Errorf("wire: unregistered message type %q", env.Type)
	}
	msg := build()
	if err := json.Unmarshal(env.Payload, msg); err != nil {
		return nil, fmt.Errorf("wire: payload for %q: %w", env.Type, err)
	}
	return &Frame{Envelope: env, Message: msg}, nil
}

// Encode renders a frame back to its JSON wire form.
func Encode(frame *Frame) ([]byte, error) {
	payload, err := json.Marshal(frame.Message)
	if err != nil {
		return nil, err
	}
	env := frame.Envelope
	env.Payload = payload
	return json.Marshal(env)
}
