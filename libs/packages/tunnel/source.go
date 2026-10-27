package tunnel

import (
	"context"
	"net"
	"net/netip"
	"sync"
	"time"
)

// Per-source limits on the listener.
//
// The listener is reachable from outside and a dial costs nothing to open, so
// one address must not be able to hold every handshake slot or spend the
// plane's failed-verification budget for everybody else. Both limits key on the
// dial's source address: an IPv4 address as is, an IPv6 address by its /64,
// which is the smallest block one host can be assumed to control.
//
// They are only as good as that address. Behind a load balancer or a Service
// that rewrites the source (externalTrafficPolicy: Cluster, an L7 proxy), every
// agent shares a handful of node addresses, and a limit keyed on them throttles
// legitimate agents alongside whoever is misbehaving. Such a deployment should
// preserve the client address or turn these limits off.
const (
	// DefaultMaxHandshakesPerSource bounds the handshakes one source may have in
	// progress at once. A real agent holds a few tunnels per replica and dials
	// them with a backoff; sixteen concurrent handshakes from one address is a
	// rollout of many agents behind one NAT, or somebody holding slots open.
	DefaultMaxHandshakesPerSource = 16
	// DefaultSourceFailures is how many dials from one source may fail the
	// handshake or the token check within DefaultSourceFailureWindow before
	// further dials from it are answered ReasonThrottled without asking the
	// plane.
	DefaultSourceFailures = 10
	// DefaultSourceFailureWindow is the window DefaultSourceFailures counts in.
	DefaultSourceFailureWindow = time.Minute
	// maxTrackedSources bounds the memory the limits use. Past it, idle sources
	// are forgotten first; if none is idle, a new source goes untracked, which
	// errs towards answering.
	maxTrackedSources = 4096
)

type sourceKey struct{}

// WithDialSource returns ctx carrying the source a dial came from, in the form
// the per-source limits key on.
func WithDialSource(ctx context.Context, source string) context.Context {
	return context.WithValue(ctx, sourceKey{}, source)
}

// DialSource is the source of the dial a token is being verified for, or empty
// when the context carries none. A Verifier passes it to the plane, so the
// plane can key its own failed-guess budget on the agent's address, not
// on the gateway's credential, which every dial shares.
func DialSource(ctx context.Context) string {
	source, _ := ctx.Value(sourceKey{}).(string)
	return source
}

// sourceOf is the key the per-source limits use for a remote address.
func sourceOf(addr net.Addr) string {
	if addr == nil {
		return ""
	}
	ap, err := netip.ParseAddrPort(addr.String())
	if err != nil {
		host, _, splitErr := net.SplitHostPort(addr.String())
		if splitErr != nil {
			return addr.String()
		}
		return host
	}
	ip := ap.Addr().Unmap()
	if ip.Is4() {
		return ip.String()
	}
	prefix, err := ip.Prefix(64)
	if err != nil {
		return ip.String()
	}
	return prefix.String()
}

// sourceGuard holds the per-source counts.
type sourceGuard struct {
	maxInFlight int
	maxFailures int
	window      time.Duration
	now         func() time.Time

	mu      sync.Mutex
	sources map[string]*sourceState
}

type sourceState struct {
	inFlight int
	// failures are the times of recent failures, oldest first, never more than
	// maxFailures of them.
	failures []time.Time
}

func newSourceGuard(maxInFlight, maxFailures int, window time.Duration, now func() time.Time) *sourceGuard {
	return &sourceGuard{
		maxInFlight: maxInFlight,
		maxFailures: maxFailures,
		window:      window,
		now:         now,
		sources:     make(map[string]*sourceState),
	}
}

// enter claims a handshake slot for source. It reports false when the source
// already has as many handshakes in progress as it may; the caller then closes
// the dial. A true answer must be paired with leave.
func (g *sourceGuard) enter(source string) bool {
	if source == "" || g.maxInFlight < 0 {
		return true
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	state := g.stateLocked(source)
	if state == nil {
		return true
	}
	if state.inFlight >= g.maxInFlight {
		return false
	}
	state.inFlight++
	return true
}

// leave gives back a slot enter claimed, and forgets the source once it has
// nothing in progress and nothing recent against it.
func (g *sourceGuard) leave(source string) {
	if source == "" || g.maxInFlight < 0 {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	state, ok := g.sources[source]
	if !ok {
		return
	}
	if state.inFlight > 0 {
		state.inFlight--
	}
	g.expireLocked(state)
	if state.inFlight == 0 && len(state.failures) == 0 {
		delete(g.sources, source)
	}
}

// throttled reports whether source has failed too often within the window.
func (g *sourceGuard) throttled(source string) bool {
	if source == "" || g.maxFailures < 0 {
		return false
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	state, ok := g.sources[source]
	if !ok {
		return false
	}
	g.expireLocked(state)
	return len(state.failures) >= g.maxFailures
}

// fail records one failed handshake or token check from source.
func (g *sourceGuard) fail(source string) {
	if source == "" || g.maxFailures < 0 {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	state := g.stateLocked(source)
	if state == nil {
		return
	}
	g.expireLocked(state)
	if len(state.failures) >= g.maxFailures {
		state.failures = state.failures[1:]
	}
	state.failures = append(state.failures, g.now())
}

func (g *sourceGuard) expireLocked(state *sourceState) {
	cutoff := g.now().Add(-g.window)
	drop := 0
	for drop < len(state.failures) && !state.failures[drop].After(cutoff) {
		drop++
	}
	state.failures = state.failures[drop:]
}

// stateLocked returns source's state, creating it if there is room, or nil when
// the table is full of sources that are all still busy or failing.
func (g *sourceGuard) stateLocked(source string) *sourceState {
	if state, ok := g.sources[source]; ok {
		return state
	}
	if len(g.sources) >= maxTrackedSources {
		for key, state := range g.sources {
			g.expireLocked(state)
			if state.inFlight == 0 && len(state.failures) == 0 {
				delete(g.sources, key)
			}
		}
		if len(g.sources) >= maxTrackedSources {
			return nil
		}
	}
	state := &sourceState{}
	g.sources[source] = state
	return state
}
