package tunnel

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"time"
)

// The handshake is one exchange, before either end speaks HTTP/2, and it
// carries the only identity the data path has: an agent presents the
// per-cluster enrolment token, and the gateway answers with the cluster that
// token resolved to.
//
// It is length-prefixed rather than line- or HTTP-shaped so that neither end
// needs a buffered reader. A bufio.Reader over the raw connection would happily
// pull the peer's first HTTP/2 bytes into its buffer along with the handshake,
// and those bytes are then invisible to the http2 code that is handed the
// connection — a truncated client preface, at a rate that depends on how the
// dial happened to be packetised.
const (
	// magic identifies the protocol and its version. A version bump is a new
	// magic, so an agent that speaks the wrong one is rejected before it can
	// be misread rather than after.
	magic = "HGRTUN01"
	// maxHandshakeBytes caps the frame, so an unauthenticated dial cannot make
	// the gateway allocate.
	maxHandshakeBytes = 8 << 10
)

// DefaultHandshakeTimeout bounds the whole exchange, including the plane call
// that verifies the token.
//
// Without it an unauthenticated dial that says nothing holds a goroutine and a
// file descriptor for as long as it likes, which is the cheapest denial of
// service there is against a listener that is deliberately reachable from
// outside.
const DefaultHandshakeTimeout = 10 * time.Second

// ErrHandshake means a dial did not present a readable handshake. It is
// distinct from ErrRejected: nothing was authenticated because nothing
// intelligible arrived.
var ErrHandshake = errors.New("tunnel: unreadable handshake")

// ErrRejected means the token was definitively refused — the plane answered,
// and the answer was no.
//
// The sentinel lives here rather than in the package that talks to the plane
// because the listener is what has to tell a rejection from a plane it could
// not reach: a rejection closes the dial, and a plane that is down falls back
// to what it already knows. Conflating them would either lock every cluster out
// during a control-plane outage or keep a revoked token working through one.
var ErrRejected = errors.New("tunnel: enrolment token rejected")

// ErrRefused means the gateway turned the dial away for a reason that is not
// about the credential: the plane could not be reached to check it, the
// gateway is at its tunnel limit, or the dial did not complete. Unlike
// ErrRejected it is worth retrying with a backoff.
var ErrRefused = errors.New("tunnel: dial refused")

// ErrDuplicate means the dial reached a gateway replica the agent said it
// already holds its quota of tunnels to. It is ordinary: the address is load
// balanced and the agent cannot choose the replica. The Accept that comes with
// it still names the replica and the replica count.
var ErrDuplicate = errors.New("tunnel: replica already covered")

// Hello is what an agent presents when it dials in.
type Hello struct {
	// Token is the cluster's enrolment token, the same value the operator
	// presents to the hub. It is the whole of the agent's identity.
	Token string `json:"token"`
	// ClusterID, when set, is the cluster the agent believes it is. It is
	// not trusted: identity is whatever the token resolves to. It is checked
	// against that answer, because an agent whose Secret was copied from
	// another cluster's would otherwise quietly serve that cluster's traffic.
	ClusterID string `json:"cluster_id,omitempty"`
	// Agent names the pod, for logs. Never a metric label.
	Agent string `json:"agent,omitempty"`
	// Covered lists the gateway replicas, by GatewayID, this agent already holds
	// its quota of tunnels to. A replica that finds itself here answers
	// ReasonDuplicate rather than accepting a tunnel the agent would close at
	// once, which the gateway would otherwise count as a failed dial.
	Covered []string `json:"covered,omitempty"`
}

// Accept is the gateway's answer. An agent that reads OK false must not
// retry immediately: back off, and for ReasonUnauthorized expect nothing to
// change until its credential does.
type Accept struct {
	OK bool `json:"ok"`
	// Reason is why a dial was refused, one of the Reason constants. Empty when
	// OK.
	Reason    string `json:"reason,omitempty"`
	ClusterID string `json:"cluster_id,omitempty"`
	// ClusterName is the same cluster under the name its own configuration uses,
	// sent so that an agent can recognise the answer as being about itself.
	//
	// An agent claims the name — the chart's `cluster.id` is what the plane
	// rendered, which is the name — while ClusterID above is the uid, because
	// that is the identifier the pool and every metric label key on. Without
	// this field the two halves have nothing in common to compare, and the
	// agent's own safety check (a Secret holding another cluster's credential)
	// cannot tell that case apart from the ordinary one.
	ClusterName string `json:"cluster_name,omitempty"`
	Error       string `json:"error,omitempty"`
	// GatewayID names the replica that accepted this dial. Stable for the life
	// of that replica and the same on every connection it accepts, which is what
	// lets an agent tell a replica it has never reached from one it already holds
	// a tunnel to. It is the pod name, so it is also what the gateway's own logs
	// say.
	GatewayID string `json:"gateway_id,omitempty"`
	// Gateways is how many replicas the gateway is running, and it is here rather
	// than in an API call on purpose.
	//
	// An agent has to hold a tunnel to every replica — a replica with no tunnel
	// to a cluster cannot serve that cluster, and looks healthy from everywhere
	// else — so it has to know how many there are. Asking the plane for the
	// number would put the control plane between a cluster and the recovery of
	// its own data path. Answering it on
	// the connection the agent has already established costs nothing and cannot
	// be unavailable.
	//
	// Zero means the gateway did not say, and the agent must read that as one.
	Gateways int `json:"gateways,omitempty"`
}

// Handshake presents a hello on an already-connected socket and reads the
// answer. It is the agent's half; the gateway's is ReadHello and
// WriteAccept.
//
// On return the connection is positioned exactly at the first HTTP/2 byte, and
// the caller may hand it straight to a Connector.
func Handshake(ctx context.Context, conn net.Conn, hello Hello) (Accept, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(DefaultHandshakeTimeout)
	}
	if err := conn.SetDeadline(deadline); err != nil {
		return Accept{}, err
	}
	defer func() { _ = conn.SetDeadline(time.Time{}) }()

	if err := writeFrame(conn, []byte(magic), hello); err != nil {
		return Accept{}, err
	}
	var accept Accept
	if err := readFrame(conn, nil, &accept); err != nil {
		return Accept{}, err
	}
	if !accept.OK {
		return accept, refusal(accept)
	}
	return accept, nil
}

// refusal maps a refused Accept to the sentinel the agent acts on. Only a
// refused credential is ErrRejected; a plane the gateway could not reach is not
// the agent's fault and must not read as one.
func refusal(accept Accept) error {
	reason := accept.Reason
	if reason == "" {
		reason = accept.Error
	}
	switch reason {
	case ReasonUnauthorized:
		return fmt.Errorf("%w: %s", ErrRejected, reason)
	case ReasonDuplicate:
		return fmt.Errorf("%w: %s", ErrDuplicate, accept.GatewayID)
	default:
		return fmt.Errorf("%w: %s", ErrRefused, reason)
	}
}

// ReadHello reads an agent's handshake off a raw connection.
//
// It takes an io.Reader rather than the net.Conn so that nothing in here can
// touch the deadline or the socket: exactly one frame is read, and positioning
// the connection for HTTP/2 is the whole contract.
func ReadHello(r io.Reader) (Hello, error) {
	var hello Hello
	if err := readFrame(r, []byte(magic), &hello); err != nil {
		return Hello{}, err
	}
	if hello.Token == "" {
		return Hello{}, fmt.Errorf("%w: no token", ErrHandshake)
	}
	return hello, nil
}

// WriteAccept answers a handshake.
func WriteAccept(w io.Writer, accept Accept) error {
	return writeFrame(w, nil, accept)
}

func writeFrame(w io.Writer, prefix []byte, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	if len(body) > maxHandshakeBytes {
		return fmt.Errorf("%w: %d bytes is past the %d cap", ErrHandshake, len(body), maxHandshakeBytes)
	}
	frame := make([]byte, 0, len(prefix)+4+len(body))
	frame = append(frame, prefix...)
	frame = binary.BigEndian.AppendUint32(frame, uint32(len(body)))
	frame = append(frame, body...)
	if _, err := w.Write(frame); err != nil {
		return err
	}
	return nil
}

// readFrame reads exactly one frame and not a byte more, which is what lets the
// same connection carry HTTP/2 afterwards.
func readFrame(r io.Reader, prefix []byte, payload any) error {
	if len(prefix) > 0 {
		got := make([]byte, len(prefix))
		if _, err := io.ReadFull(r, got); err != nil {
			return fmt.Errorf("%w: %w", ErrHandshake, err)
		}
		if string(got) != string(prefix) {
			return fmt.Errorf("%w: not a hangar tunnel dial", ErrHandshake)
		}
	}

	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return fmt.Errorf("%w: %w", ErrHandshake, err)
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > maxHandshakeBytes {
		return fmt.Errorf("%w: frame of %d bytes", ErrHandshake, size)
	}
	body := make([]byte, size)
	if _, err := io.ReadFull(r, body); err != nil {
		return fmt.Errorf("%w: %w", ErrHandshake, err)
	}
	if err := json.Unmarshal(body, payload); err != nil {
		return fmt.Errorf("%w: %w", ErrHandshake, err)
	}
	return nil
}
