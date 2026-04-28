package tunnel

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"syscall"

	"golang.org/x/net/http2"
)

// Fault is why a tunnelled request did not complete, from a closed set.
//
// It exists because the gateway's existing classification cannot see any of
// this. markProxyError distinguishes context.Canceled and net.Error timeouts and
// calls everything else an upstream error, and neither http2.StreamError nor
// http2.GoAwayError is either of those — so an agent redeploy, which is a
// routine rollout of a data-plane pod, would be recorded as the cluster failing
// and would deflate the serving availability that is quoted to a buyer.
//
// The recommended mapping into metrics.Outcome, for whoever wires this up:
//
//	FaultNone          -> from the status code, as today
//	FaultNoTunnel      -> OutcomeNoUpstream    (nothing was attempted)
//	FaultSaturated     -> OutcomeShed          (this hop refused, to stay fast)
//	FaultChurn         -> OutcomeUpstreamError, for now
//	FaultHeaderTimeout -> OutcomeUpstreamTimeout
//	FaultCanceled      -> OutcomeCanceled
//	FaultUpstream      -> OutcomeUpstreamError
//
// Churn folding into OutcomeUpstreamError is a decision, not an oversight. A
// churn outcome that sat in availability's denominator but never its numerator
// would inflate the number unfalsifiably, which shared-decisions §3.2 forbids by
// name, and splitting it honestly means minting a new frozen objective version.
// So an agent rollout counts slightly against serving availability: pessimistic
// and honest, in that order. This distinction is still worth drawing here, because
// it is what a fault label on a log line or a gateway-local counter needs.
type Fault int

const (
	// FaultNone is no error.
	FaultNone Fault = iota
	// FaultNoTunnel is a cluster with no agent attached. Nothing was sent.
	FaultNoTunnel
	// FaultSaturated is every tunnel to the cluster at its stream limit.
	FaultSaturated
	// FaultChurn is the tunnel itself ending under a request: GOAWAY, a stream
	// reset, a connection that went away. Attributable to the agent, which
	// is data-plane infrastructure this project operates — not to the cluster
	// and not to the model.
	FaultChurn
	// FaultHeaderTimeout is a stream the cluster accepted and never answered.
	FaultHeaderTimeout
	// FaultCanceled is the caller hanging up.
	FaultCanceled
	// FaultUpstream is the cluster genuinely failing.
	FaultUpstream
)

func (f Fault) String() string {
	switch f {
	case FaultNone:
		return "none"
	case FaultNoTunnel:
		return "no_tunnel"
	case FaultSaturated:
		return "saturated"
	case FaultChurn:
		return "tunnel_churn"
	case FaultHeaderTimeout:
		return "header_timeout"
	case FaultCanceled:
		return "canceled"
	default:
		return "upstream"
	}
}

// Retryable reports whether the same request could be sent again. It describes
// the fault, not a policy: HTTP/2 has already done its own retrying by the time
// an error reaches here.
func (f Fault) Retryable() bool {
	return f == FaultChurn || f == FaultSaturated || f == FaultNoTunnel
}

// churnSentinels are x/net's unexported connection-lifecycle errors, by message.
//
// Every one of them means the tunnel ended, and every one is a plain errors.New
// inside x/net with no type to match on. The retry sentinels only escape when
// HTTP/2's own retry budget is exhausted, which the pool makes unlikely by
// refusing a request outright once a cluster has no live tunnel; the lost-ping
// and force-close messages reach any request that was in flight when a tunnel
// died. Matching on the text is unpleasant and it is here anyway: the
// alternative is that the errors which most obviously mean "an agent went
// away" are filed under "the cluster is down".
var churnSentinels = []string{
	"http2: client conn not usable",
	closedConnSentinel,
	"http2: client conn could not be established",
	lostConnSentinel,
	forceClosedSentinel,
	"http2: Transport received Server's graceful shutdown GOAWAY",
	"http2: Transport received GOAWAY from server",
}

// forceClosedSentinel is x/net's errClientConnForceClosed, which is what an
// in-flight request gets when the write half of its connection fails and
// cleanupWriteRequest tears the connection down under it. Unexported there, and
// the exact ending of an agent that was killed rather than drained.
const forceClosedSentinel = "http2: client connection force closed"

// closedConnSentinel is x/net's errClientConnClosed, returned from
// awaitFlowControl to a request that was mid-write when its connection was found
// closed. It is the narrow race between the pool picking a live tunnel and the
// tunnel dying — rare, and reached under load about once in twenty runs before it
// was covered.
const closedConnSentinel = "http2: client conn is closed"

// lostConnSentinel is x/net's errClientConnLost, which reaches a request whose
// connection ended while it was waiting for the response.
const lostConnSentinel = "http2: client connection lost"

// isTunnelDeath reports whether an error is the tunnel socket itself ending
// rather than the cluster behind it failing.
//
// EPIPE and ECONNRESET are the whole reason this is separate from the sentinel
// list. They arrive as *net.OpError, they are what a SIGKILLed or OOMKilled
// agent produces — the process stops existing and its socket is reset with no
// GOAWAY on it — and taken at face value they read as a network failure, which
// on this connection would mean the cluster. It cannot: the agent proxies to
// the cluster inside its own process and reports a cluster that will not answer
// as a 502 status. A syscall error on this socket only ever means the near end of
// the tunnel died.
//
// A killed process does not always reset its sockets: the kernel reaps the fd,
// and with nothing left unread on it that is an ordinary FIN. The gateway sees
// that as an unexpected EOF rather than a syscall error, so it belongs here too,
// and it is the deterministic half of the pair — an RST can be dropped by the
// peer's TCP if it arrives outside the receive window, a FIN cannot.
//
// The named sentinels are here rather than only in churnSentinels because these
// are the endings the pool retries, and canRetryError accepts none of them.
func isTunnelDeath(err error) bool {
	if errors.Is(err, syscall.EPIPE) || errors.Is(err, syscall.ECONNRESET) {
		return true
	}
	if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return true
	}
	message := err.Error()
	for _, sentinel := range []string{forceClosedSentinel, closedConnSentinel, lostConnSentinel} {
		if strings.Contains(message, sentinel) {
			return true
		}
	}
	return false
}

// Classify says what a failed tunnelled request means.
//
// Order matters. A cancelled request often surfaces as a stream error too — the
// stream is reset because the caller went away — and blaming the tunnel for a
// caller's disconnect would be the same misattribution in the other direction.
func Classify(err error) Fault {
	if err == nil {
		return FaultNone
	}

	switch {
	case errors.Is(err, ErrNoTunnel), errors.Is(err, ErrNoCluster):
		return FaultNoTunnel
	case errors.Is(err, ErrSaturated):
		return FaultSaturated
	}

	if errors.Is(err, context.Canceled) || isRequestCanceled(err) {
		return FaultCanceled
	}
	if errors.Is(err, context.DeadlineExceeded) || isNetTimeout(err) {
		return FaultHeaderTimeout
	}

	var goAway http2.GoAwayError
	if errors.As(err, &goAway) {
		return FaultChurn
	}
	// TODO: read the agent's cause trailer here instead of guessing.
	// An agent dying and an agent's upstream dying both arrive as
	// StreamError{INTERNAL_ERROR, received from peer}, so this branch cannot tell
	// them apart and calls both churn. The additive fix is an HTTP/2 trailer set
	// by the agent naming the cause — see the matching TODO in agent.go —
	// read from resp.Trailer on the body-error path, with absence falling back to
	// exactly what happens here. Until then an agent rollout counts slightly
	// against serving availability, which is pessimistic and honest.
	var stream http2.StreamError
	if errors.As(err, &stream) {
		return FaultChurn
	}
	var connErr http2.ConnectionError
	if errors.As(err, &connErr) {
		return FaultChurn
	}
	if errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return FaultChurn
	}
	if isTunnelDeath(err) {
		return FaultChurn
	}
	for _, sentinel := range churnSentinels {
		if strings.Contains(err.Error(), sentinel) {
			return FaultChurn
		}
	}
	return FaultUpstream
}

// isRequestCanceled matches net/http's unexported errRequestCanceled, which
// x/net copies rather than exports.
func isRequestCanceled(err error) bool {
	return strings.Contains(err.Error(), "net/http: request canceled")
}

func isNetTimeout(err error) bool {
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}
