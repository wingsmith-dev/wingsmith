package tunnel

import "time"

// Limits are the HTTP/2 settings both ends of a tunnel apply.
//
// Every value is set explicitly, including the ones whose x/net default is
// already the value we want. A flow-control window that comes from a
// dependency's default is a capacity decision made by whoever last bumped
// go.mod, and the symptom of it changing is throughput on a long generation,
// which is not a symptom anybody traces back to a module upgrade.
type Limits struct {
	// MaxConcurrentStreams is how many requests one tunnel carries at once. It
	// is advertised by the agent and learned by the gateway from SETTINGS.
	//
	// It is a per-tunnel admission limit, not a tuning knob: past it the pool
	// answers ErrSaturated instead of queueing, so this number and the number
	// of tunnels a cluster holds together decide when the gateway says "no
	// capacity" instead of getting slower.
	MaxConcurrentStreams uint32
	// ConnWindow is the connection-level receive window, in bytes.
	ConnWindow int32
	// StreamWindow is the per-stream receive window, in bytes.
	StreamWindow int32
	// MaxReadFrameSize is the largest frame payload either end will accept.
	MaxReadFrameSize uint32
	// ReadIdleTimeout is how long a silent connection goes before a PING is
	// sent. This is the only thing that notices a peer that vanished without a
	// FIN: a node that lost power, a NAT that dropped the mapping, a security
	// group that changed under a live connection.
	ReadIdleTimeout time.Duration
	// PingTimeout is how long a PING may go unanswered before the connection is
	// declared dead and torn down.
	PingTimeout time.Duration
	// WriteByteTimeout is how long a write may make no progress. It bounds the
	// damage from a peer that stops reading, which on a tunnel means one
	// wedged agent holding gateway memory for every stream on it.
	WriteByteTimeout time.Duration
}

// DefaultLimits are what a tunnel uses when nothing overrides it.
//
// The connection window is sixteen stream windows, and that ratio is the point.
// A response the gateway has not forwarded holds its share of the connection
// window until somebody reads it, so with the connection window only a few
// stream windows wide, a few callers that stop reading stall every other stream
// on the tunnel. Sixteen buys room for that while the pool's body idle timeout
// cuts the stuck streams loose, and it stays far below x/net's client-side
// 1 GiB default: one tunnel multiplexes every request to a cluster, so the
// connection window is memory the gateway may be asked to hold per tunnel.
func DefaultLimits() Limits {
	return Limits{
		MaxConcurrentStreams: 256,
		ConnWindow:           64 << 20,
		StreamWindow:         4 << 20,
		MaxReadFrameSize:     1 << 20,
		ReadIdleTimeout:      15 * time.Second,
		PingTimeout:          10 * time.Second,
		WriteByteTimeout:     30 * time.Second,
	}
}

// withDefaults fills anything left at zero.
func (l Limits) withDefaults() Limits {
	defaults := DefaultLimits()
	if l.MaxConcurrentStreams == 0 {
		l.MaxConcurrentStreams = defaults.MaxConcurrentStreams
	}
	if l.ConnWindow <= 0 {
		l.ConnWindow = defaults.ConnWindow
	}
	if l.StreamWindow <= 0 {
		l.StreamWindow = defaults.StreamWindow
	}
	if l.MaxReadFrameSize == 0 {
		l.MaxReadFrameSize = defaults.MaxReadFrameSize
	}
	if l.ReadIdleTimeout == 0 {
		l.ReadIdleTimeout = defaults.ReadIdleTimeout
	}
	if l.PingTimeout == 0 {
		l.PingTimeout = defaults.PingTimeout
	}
	if l.WriteByteTimeout == 0 {
		l.WriteByteTimeout = defaults.WriteByteTimeout
	}
	return l
}
