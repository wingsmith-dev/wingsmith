package tunnel

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net"
	"slices"
	"sync"
	"time"
)

// Identity is the cluster a token belongs to, under both of its names.
//
// Two fields and not one because a cluster is addressable two ways and the two
// halves of this system reach for different ones. ClusterID is the plane's uid
// and is what everything downstream keys on (the pool, the routing table, every
// metric label) because it is the only one that cannot be renamed. ClusterName
// is the operator-facing name, and it is what a cluster's own configuration
// carries: the chart's `cluster.id`, the operator's WINGSMITH_CLUSTER_ID, the label
// its metrics are written under. An agent therefore claims the name, and a
// listener comparing that claim against the uid alone refuses every real
// install.
type Identity struct {
	ClusterID   string
	ClusterName string
}

// claims reports whether an agent's claimed cluster is this identity.
//
// Either spelling is accepted, for the same reason `GET /v1/clusters/{ref}`
// takes either: they name one row, and which one a caller holds is an accident
// of where it read it. Empty claims nothing and is not a match: the caller
// decides whether an unclaimed dial is allowed.
func (i Identity) claims(claimed string) bool {
	if claimed == "" {
		return false
	}
	return claimed == i.ClusterID || claimed == i.ClusterName
}

// Verifier resolves an enrolment token to the cluster it belongs to.
//
// The gateway holds no cluster token material: it has no list of hashes, no
// database, and nothing to keep in sync with the plane. It asks when a tunnel
// opens and again every ReverifyInterval while it stays open, and remembers the
// answer for CacheTTL. That is the whole of the trust model on this listener:
// TLS for confidentiality and the gateway's identity, the token for the agent's.
// Agents present no client certificate, so there is no client CA and no mTLS.
//
// A Verifier returns ErrRejected only for a definitive no. Anything else is
// read as "the plane could not be reached", which is a different situation with
// a different answer.
type Verifier interface {
	VerifyTunnelToken(ctx context.Context, token string) (Identity, error)
}

// DialObserver counts dials that never became tunnels.
//
// Separate from Observer because the pool never sees these: a dial that fails
// the handshake or the token check is not a tunnel that opened and closed, and
// folding it into the coverage gauge would make a misconfigured agent look
// like a flapping one. The reason strings are a closed set (see the Reason
// constants) because this is a metric label.
type DialObserver interface {
	TunnelDialRejected(reason string)
}

// Reasons a dial was refused. Closed set: these reach a metric label.
const (
	// ReasonHandshake is a dial that presented nothing intelligible.
	ReasonHandshake = "handshake"
	// ReasonUnauthorized is a token the plane refused, or one that resolved to a
	// cluster other than the one the agent claimed.
	ReasonUnauthorized = "unauthorized"
	// ReasonUnavailable is a token that could not be checked because the plane
	// could not be reached and nothing was cached for it. Existing tunnels are
	// unaffected; only new dials land here.
	ReasonUnavailable = "unavailable"
	// ReasonAdopt is an agent that authenticated and then failed to speak
	// HTTP/2.
	ReasonAdopt = "adopt"
	// ReasonLimit is a dial that authenticated while its cluster, or this
	// replica, already held as many tunnels as the pool allows.
	ReasonLimit = "limit"
	// ReasonDuplicate is a dial to a replica the agent said it already covers.
	// It is answered with this reason but is not a refusal and is not counted
	// as one; see DuplicateObserver.
	ReasonDuplicate = "duplicate"
	// ReasonThrottled is a dial from a source that has failed too many
	// handshakes or token checks recently, or that already has too many
	// handshakes in progress. The plane is not asked about it.
	ReasonThrottled = "throttled"
)

// DuplicateObserver is optionally implemented by a DialObserver to count dials
// that landed on a replica the agent already covers. They are kept apart from
// refusals because they are the normal cost of finding every replica behind a
// load-balanced address, not a dial that failed.
type DuplicateObserver interface {
	TunnelDialDuplicate()
}

// ErrListenerClosed is returned by Serve on a listener that has been shut down
// or closed.
var ErrListenerClosed = errors.New("tunnel: listener closed")

// DefaultMaxHandshakes bounds how many dials may be mid-handshake at once.
//
// The listener is reachable from outside by design and a dial costs nothing to
// open, so without a bound a flood of connections that never speak holds a
// goroutine and a descriptor each until the handshake timeout. Past the bound
// the accept loop waits; the kernel's backlog holds the dials, and an agent's
// own dial timeout decides how long it waits.
const DefaultMaxHandshakes = 128

// RevocationObserver is optionally implemented by a DialObserver to count
// tunnels closed because their token stopped verifying.
type RevocationObserver interface {
	TunnelRevoked(clusterID string)
}

// DefaultCacheTTL is how long a verified token is reused without asking the
// plane again. It bounds how long a revoked token keeps opening tunnels, and
// with DefaultReverifyInterval how long one keeps an open tunnel.
const DefaultCacheTTL = time.Minute

// DefaultReverifyInterval is how often the token behind an open tunnel is
// checked again. A tunnel whose token the plane now refuses, or now resolves to
// a different cluster, is closed. One that cannot be checked because the plane
// is unreachable is kept: an outage of the control plane must not take the data
// path with it.
const DefaultReverifyInterval = time.Minute

// DefaultCacheGrace is how long a verified token stays usable after the plane
// has become unreachable.
//
// It is what makes a control-plane outage survivable for an agent that is
// restarted during one. It is bounded instead
// of infinite because a token that has been revoked has to stop working
// eventually, even for new dials during an outage.
const DefaultCacheGrace = 15 * time.Minute

// ListenerConfig configures the tunnel listener.
type ListenerConfig struct {
	// Pool adopts every connection that authenticates.
	Pool *Pool
	// GatewayID is this replica's stable identity, reported to every agent that
	// dials in. Empty means the agent cannot tell replicas apart and will treat
	// the gateway as a single one.
	GatewayID string
	// Gateways is how many replicas the gateway is running, reported to every
	// agent that dials in so it knows how many distinct tunnels to hold. Zero or
	// one both mean one.
	//
	// It is a number this process is told instead of one it discovers. The
	// gateway has no Kubernetes client and is not getting one, so the manifest that sets the replica count is
	// also what sets this, and the two live beside each other in one file.
	Gateways int
	// Verifier checks enrolment tokens. Required: a listener with no verifier
	// would accept any dial that spoke the protocol.
	Verifier Verifier
	// HandshakeTimeout bounds the exchange and the verification together. Zero
	// means DefaultHandshakeTimeout.
	HandshakeTimeout time.Duration
	// CacheTTL is how long a verified token is trusted without asking again.
	// Zero means DefaultCacheTTL.
	CacheTTL time.Duration
	// CacheGrace is how long a verified token survives a plane that cannot be
	// reached. Zero means DefaultCacheGrace.
	CacheGrace time.Duration
	// ReverifyInterval is how often an open tunnel's token is checked again.
	// Zero means DefaultReverifyInterval; negative turns re-verification off.
	ReverifyInterval time.Duration
	// MaxHandshakes bounds how many dials may be mid-handshake at once. Zero
	// means DefaultMaxHandshakes.
	MaxHandshakes int
	// MaxHandshakesPerSource bounds how many of those may come from one source
	// address. A dial past it is closed as soon as it is accepted. Zero means
	// DefaultMaxHandshakesPerSource; negative turns the bound off.
	MaxHandshakesPerSource int
	// SourceFailures is how many dials from one source may fail the handshake
	// or the token check within SourceFailureWindow before the rest are
	// answered ReasonThrottled without a plane call. A token verified within
	// CacheTTL still gets in. Zero means DefaultSourceFailures; negative turns
	// the limit off.
	SourceFailures int
	// SourceFailureWindow is the window SourceFailures counts in. Zero means
	// DefaultSourceFailureWindow.
	SourceFailureWindow time.Duration
	// Dials, when set, is told about refused dials.
	Dials DialObserver
	// Now replaces the clock. For tests.
	Now func() time.Time
}

// Listener accepts agent dials and turns the ones that authenticate into
// tunnels.
//
// It is a listener of its own, on a port of its own, and that is not tidiness:
// it must stay reachable while this replica reports itself unready. Readiness
// here means "holds a tunnel", so a replica that could only be dialled once
// ready could never become ready: the gate would be the thing keeping it shut.
// Nothing in this file consults readiness, and the Service in front of this port
// must publish not-ready addresses.
type Listener struct {
	cfg     ListenerConfig
	cache   *tokenCache
	sources *sourceGuard

	mu       sync.Mutex
	ln       net.Listener
	closing  bool
	handling sync.WaitGroup
	// stopped closes when the listener stops accepting. It wakes an accept loop
	// waiting for a handshake slot and cancels handshakes still in progress.
	stopped chan struct{}
	// life ends with the listener, not with one Serve call, and bounds
	// the re-verification of open tunnels. A Serve that returns on an accept
	// error leaves its tunnels open, and they must go on being checked.
	life    context.Context
	endLife context.CancelFunc
}

// NewListener builds the listener. It does not bind: the caller supplies the
// net.Listener, so TLS and the address stay wiring decisions.
func NewListener(cfg ListenerConfig) (*Listener, error) {
	if cfg.Pool == nil {
		return nil, errors.New("tunnel: listener needs a pool")
	}
	if cfg.Verifier == nil {
		return nil, errors.New("tunnel: listener needs a verifier")
	}
	if cfg.HandshakeTimeout <= 0 {
		cfg.HandshakeTimeout = DefaultHandshakeTimeout
	}
	if cfg.CacheTTL <= 0 {
		cfg.CacheTTL = DefaultCacheTTL
	}
	if cfg.CacheGrace <= 0 {
		cfg.CacheGrace = DefaultCacheGrace
	}
	if cfg.MaxHandshakes <= 0 {
		cfg.MaxHandshakes = DefaultMaxHandshakes
	}
	if cfg.ReverifyInterval == 0 {
		cfg.ReverifyInterval = DefaultReverifyInterval
	}
	if cfg.MaxHandshakesPerSource == 0 {
		cfg.MaxHandshakesPerSource = DefaultMaxHandshakesPerSource
	}
	if cfg.SourceFailures == 0 {
		cfg.SourceFailures = DefaultSourceFailures
	}
	if cfg.SourceFailureWindow <= 0 {
		cfg.SourceFailureWindow = DefaultSourceFailureWindow
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	life, endLife := context.WithCancel(context.Background())
	return &Listener{
		cfg:     cfg,
		cache:   &tokenCache{ttl: cfg.CacheTTL, grace: cfg.CacheGrace, now: cfg.Now},
		sources: newSourceGuard(cfg.MaxHandshakesPerSource, cfg.SourceFailures, cfg.SourceFailureWindow, cfg.Now),
		stopped: make(chan struct{}),
		life:    life,
		endLife: endLife,
	}, nil
}

// Serve accepts dials until the listener is shut down or ctx is cancelled.
//
// ctx bounds handshakes and the plane calls inside them. It does not abort
// tunnels that are already adopted: cancelling it stops new dials, and Shutdown
// is what drains the live ones.
//
// An Accept error the network marks temporary (out of file descriptors, a
// connection reset before it was accepted) is waited out with a backoff, as
// net/http does. Returning on one would take down a listener every agent
// depends on over a condition that clears by itself.
//
// Any other Accept error is returned, and Serve may then be called again with
// a new net.Listener. Tunnels adopted before it returned stay open and go on
// being re-verified; only Shutdown or Close ends them.
func (l *Listener) Serve(ctx context.Context, ln net.Listener) error {
	l.mu.Lock()
	if l.closing {
		l.mu.Unlock()
		_ = ln.Close()
		return ErrListenerClosed
	}
	l.ln = ln
	l.mu.Unlock()

	// Handshakes in progress end when the listener stops, not only when ctx does.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-l.stopped:
			cancel()
		case <-ctx.Done():
		}
	}()

	slots := make(chan struct{}, l.cfg.MaxHandshakes)
	var delay time.Duration
	for {
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
			return nil
		}
		conn, err := ln.Accept()
		if err != nil {
			<-slots
			if l.isClosing() || ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			if !temporary(err) {
				return err
			}
			delay = nextAcceptDelay(delay)
			slog.Warn("gateway: tunnel accept failed, retrying", "err", err, "retry_in", delay)
			timer := time.NewTimer(delay)
			select {
			case <-timer.C:
			case <-ctx.Done():
				timer.Stop()
				return nil
			}
			continue
		}
		delay = 0
		source := sourceOf(conn.RemoteAddr())
		if !l.sources.enter(source) {
			<-slots
			_ = conn.Close()
			l.refuse(addrString(conn), ReasonThrottled, errors.New("too many handshakes in progress from this source"))
			continue
		}
		l.handling.Add(1)
		go func() {
			defer l.handling.Done()
			var once sync.Once
			release := func() {
				once.Do(func() {
					l.sources.leave(source)
					<-slots
				})
			}
			defer release()
			l.handle(WithDialSource(ctx, source), conn, release)
		}()
	}
}

func (l *Listener) isClosing() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.closing
}

// temporary reports whether an Accept error is one to wait out.
func temporary(err error) bool {
	var t interface{ Temporary() bool }
	return errors.As(err, &t) && t.Temporary()
}

// nextAcceptDelay is net/http's accept backoff: 5ms doubling to a second.
func nextAcceptDelay(delay time.Duration) time.Duration {
	if delay == 0 {
		return 5 * time.Millisecond
	}
	delay *= 2
	if delay > time.Second {
		delay = time.Second
	}
	return delay
}

// handle authenticates one dial and hands it to the pool. release gives back
// the handshake slot, and is called as soon as the dial is a tunnel or is gone.
func (l *Listener) handle(ctx context.Context, conn net.Conn, release func()) {
	remote := addrString(conn)
	handshakeCtx, cancel := context.WithTimeout(ctx, l.cfg.HandshakeTimeout)
	defer cancel()

	clusterID, hello, err := l.authenticate(handshakeCtx, conn)
	if err != nil {
		_ = conn.Close()
		return
	}

	// Adopt closes the connection itself on failure, and owns it from here.
	adopted, err := l.cfg.Pool.Adopt(handshakeCtx, clusterID, conn)
	release()
	if err != nil {
		l.refuse(remote, ReasonAdopt, err)
		return
	}
	slog.Info("gateway: tunnel open",
		"cluster", clusterID, "agent", hello.Agent, "remote", remote,
		"capacity", adopted.Capacity(), "live", l.cfg.Pool.Live(clusterID))

	l.watchToken(WithDialSource(l.life, DialSource(ctx)), adopted, hello.Token)
	slog.Info("gateway: tunnel closed",
		"cluster", clusterID, "remote", remote,
		"held", time.Since(adopted.Opened).Round(time.Second).String(),
		"live", l.cfg.Pool.Live(clusterID))
}

// watchToken re-verifies an open tunnel's token until the tunnel ends, and
// closes the tunnel if the token is refused or now names another cluster.
//
// The token is held in memory for as long as the tunnel is open; that is the
// cost of being able to revoke it. A plane that cannot be reached keeps the
// tunnel, for the same reason it keeps new dials working from the cache.
func (l *Listener) watchToken(ctx context.Context, conn *Conn, token string) {
	if l.cfg.ReverifyInterval < 0 {
		<-conn.Done()
		return
	}
	for {
		// Jittered so the tunnels a rollout opened together do not all ask at once.
		wait := l.cfg.ReverifyInterval + time.Duration(rand.Int64N(int64(l.cfg.ReverifyInterval)/10+1))
		timer := time.NewTimer(wait)
		select {
		case <-conn.Done():
			timer.Stop()
			return
		case <-ctx.Done():
			timer.Stop()
			<-conn.Done()
			return
		case <-timer.C:
		}

		// ctx carries the dial's source, so the plane charges a refusal here to
		// the agent's address and not to the budget every dial shares.
		checkCtx, cancel := context.WithTimeout(ctx, l.cfg.HandshakeTimeout)
		identity, err := l.resolve(checkCtx, token)
		cancel()
		switch {
		case err == nil && identity.ClusterID == conn.ClusterID:
			continue
		case err == nil, errors.Is(err, ErrRejected):
			slog.Warn("gateway: tunnel token no longer valid, closing the tunnel",
				"cluster", conn.ClusterID, "remote", conn.RemoteAddr, "resolves_to", identity.ClusterID, "err", err)
			if observer, ok := l.cfg.Dials.(RevocationObserver); ok {
				observer.TunnelRevoked(conn.ClusterID)
			}
			_ = conn.Close()
			return
		default:
			slog.Debug("gateway: could not re-verify a tunnel token, keeping the tunnel",
				"cluster", conn.ClusterID, "err", err)
		}
	}
}

// Revoke closes every tunnel to a cluster and forgets every cached answer that
// named it, so the next dial with any of its tokens asks the plane. It is the
// path for a credential known to be compromised, where waiting for the next
// re-verification is too slow. It returns how many tunnels were closed.
func (l *Listener) Revoke(clusterID string) int {
	l.cache.forgetCluster(clusterID)
	return l.cfg.Pool.Evict(clusterID)
}

// authenticate reads the handshake and answers it. The deadline it sets is
// cleared before the connection is handed on: an HTTP/2 connection with a
// socket deadline on it dies at that deadline mid-generation.
func (l *Listener) authenticate(ctx context.Context, conn net.Conn) (string, Hello, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = l.cfg.Now().Add(l.cfg.HandshakeTimeout)
	}
	if err := conn.SetDeadline(deadline); err != nil {
		return "", Hello{}, err
	}
	defer func() { _ = conn.SetDeadline(time.Time{}) }()

	remote := addrString(conn)
	source := DialSource(ctx)
	hello, err := ReadHello(conn)
	if err != nil {
		l.sources.fail(source)
		l.reject(conn, remote, ReasonHandshake, err)
		return "", Hello{}, err
	}

	// A source that keeps failing is not let near the plane again until its
	// window passes. Without this, anyone who can reach this port could spend
	// the plane's failed-guess budget with junk tokens, and a re-verification
	// that meets the spent budget cannot tell a revoked token from a busy plane.
	// A token this listener verified within CacheTTL needs no plane call and
	// still gets in, which covers an agent sharing an address with whoever is
	// failing.
	if l.sources.throttled(source) {
		if _, fresh, ok := l.cache.lookup(hello.Token); !ok || !fresh {
			err := errors.New("too many failed dials from this source")
			l.reject(conn, remote, ReasonThrottled, err)
			return "", Hello{}, err
		}
	}

	identity, err := l.resolve(ctx, hello.Token)
	if err != nil {
		reason := ReasonUnavailable
		if errors.Is(err, ErrRejected) {
			reason = ReasonUnauthorized
			l.sources.fail(source)
		}
		l.reject(conn, remote, reason, err)
		return "", Hello{}, err
	}

	// The claim is checked, not trusted. An agent holding another cluster's
	// Secret is a copy-paste away, and the failure it produces (one cluster's
	// traffic served by another's models) is silent at every other layer.
	//
	// Checked against either of the cluster's names, because the one an agent
	// holds is not a choice it makes: the chart writes `cluster.id` from what the
	// plane rendered, which is the name.
	if hello.ClusterID != "" && !identity.claims(hello.ClusterID) {
		err := fmt.Errorf("%w: token belongs to %s (%s), dial claimed %s",
			ErrRejected, identity.ClusterID, identity.ClusterName, hello.ClusterID)
		l.sources.fail(source)
		l.reject(conn, remote, ReasonUnauthorized, err)
		return "", Hello{}, err
	}

	// An agent that already holds its quota here is told so, with the replica
	// count it dialled to learn, instead of being handed a tunnel it will close.
	if l.cfg.GatewayID != "" && slices.Contains(hello.Covered, l.cfg.GatewayID) {
		accept := l.accept(identity)
		accept.OK, accept.Reason, accept.Error = false, ReasonDuplicate, ReasonDuplicate
		_ = WriteAccept(conn, accept)
		if counter, ok := l.cfg.Dials.(DuplicateObserver); ok {
			counter.TunnelDialDuplicate()
		}
		return "", Hello{}, ErrDuplicate
	}

	if err := l.cfg.Pool.Admit(identity.ClusterID); err != nil {
		reason := ReasonLimit
		if errors.Is(err, ErrPoolClosed) {
			reason = ReasonUnavailable
		}
		l.reject(conn, remote, reason, err)
		return "", Hello{}, err
	}

	// The pool key is the uid, never the claim and never the name: everything past
	// this point (the pool, the routing table lookup, every metric label) keys
	// on the one identifier a rename cannot move. The accept carries both, so the
	// agent can see its own name in the answer.
	if err := WriteAccept(conn, l.accept(identity)); err != nil {
		return "", Hello{}, err
	}
	return identity.ClusterID, hello, nil
}

// accept is the answer to a dial that authenticated, carrying this replica's
// identity and the replica count alongside the cluster.
//
// The count is normalised here instead of at the agent, so that the wire never
// carries a zero that means one: an agent reading Gateways decides how many
// tunnels to hold, and a gateway that forgot to say is a gateway an agent must
// not conclude anything from.
func (l *Listener) accept(identity Identity) Accept {
	gateways := l.cfg.Gateways
	if gateways < 1 {
		gateways = 1
	}
	return Accept{
		OK:          true,
		ClusterID:   identity.ClusterID,
		ClusterName: identity.ClusterName,
		GatewayID:   l.cfg.GatewayID,
		Gateways:    gateways,
	}
}

// resolve verifies a token, preferring what is already known.
//
// A fresh cache entry answers without a plane call at all, so an agent that
// reconnects in a loop cannot turn a rollout into a load test of the control
// plane. A stale entry answers when the plane cannot be reached, which is what
// keeps a control-plane outage off the data path: an agent restarted during an
// outage still gets in, and re-verification keeps the tunnels it already has.
func (l *Listener) resolve(ctx context.Context, token string) (Identity, error) {
	if identity, fresh, ok := l.cache.lookup(token); ok && fresh {
		return identity, nil
	}

	identity, err := l.cfg.Verifier.VerifyTunnelToken(ctx, token)
	if err == nil {
		if identity.ClusterID == "" {
			return Identity{}, fmt.Errorf("%w: the plane named no cluster", ErrRejected)
		}
		l.cache.store(token, identity)
		return identity, nil
	}
	if errors.Is(err, ErrRejected) {
		l.cache.forget(token)
		return Identity{}, err
	}
	if identity, _, ok := l.cache.lookup(token); ok {
		slog.Warn("gateway: tunnel token verified from cache, the plane is unreachable", "err", err)
		return identity, nil
	}
	return Identity{}, err
}

// reject answers a refused dial, so an agent logs why instead of an EOF.
// The caller closes the connection.
func (l *Listener) reject(conn net.Conn, remote, reason string, err error) {
	_ = WriteAccept(conn, Accept{Reason: reason, Error: reason})
	l.refuse(remote, reason, err)
}

func (l *Listener) refuse(remote, reason string, err error) {
	// A throttled source is one already logged failing; logging each of its
	// dials again would let it fill the log instead.
	level := slog.LevelWarn
	if reason == ReasonThrottled {
		level = slog.LevelDebug
	}
	slog.Log(context.Background(), level, "gateway: tunnel dial refused", "remote", remote, "reason", reason, "err", err)
	if l.cfg.Dials != nil {
		l.cfg.Dials.TunnelDialRejected(reason)
	}
}

// Shutdown stops accepting dials and drains the tunnels already held.
//
// Both halves matter to a rollout. Closing the accept side alone would leave
// agents' streams cut when the process exits; draining alone would let a
// terminating replica keep taking dials it is about to abandon.
func (l *Listener) Shutdown(ctx context.Context) error {
	l.stopAccepting()
	err := l.cfg.Pool.Shutdown(ctx)
	done := make(chan struct{})
	go func() {
		l.handling.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
		if err == nil {
			err = ctx.Err()
		}
	}
	return err
}

// Close stops accepting and severs every tunnel. It is the deadline behind
// Shutdown, not an alternative to it.
func (l *Listener) Close() error {
	l.stopAccepting()
	return l.cfg.Pool.Close()
}

func (l *Listener) stopAccepting() {
	l.mu.Lock()
	if !l.closing {
		close(l.stopped)
		l.endLife()
	}
	l.closing = true
	ln := l.ln
	l.mu.Unlock()
	if ln != nil {
		_ = ln.Close()
	}
}

// tokenCache remembers which cluster a token resolved to.
//
// A slice scanned in constant time instead of a map: the fleet is tens of
// clusters, the scan is per dial and per re-verification and never per request,
// and it means
// no comparison in this path can be short-circuited on a secret. The tokens
// themselves are not kept here, only their digests. The tokens of open tunnels
// are held by the goroutine re-verifying each one, and no longer than that.
type tokenCache struct {
	ttl   time.Duration
	grace time.Duration
	now   func() time.Time

	mu      sync.Mutex
	entries []cacheEntry
}

type cacheEntry struct {
	digest   [sha256.Size]byte
	identity Identity
	verified time.Time
}

// lookup reports what a token resolved to last time, and whether that answer is
// still fresh enough to use without asking the plane.
func (c *tokenCache) lookup(token string) (Identity, bool, bool) {
	digest := sha256.Sum256([]byte(token))
	now := c.now()

	c.mu.Lock()
	defer c.mu.Unlock()

	kept := c.entries[:0]
	var found cacheEntry
	hit := 0
	for _, entry := range c.entries {
		if now.Sub(entry.verified) > c.grace {
			continue
		}
		kept = append(kept, entry)
		if subtle.ConstantTimeCompare(digest[:], entry.digest[:]) == 1 {
			found = entry
			hit = 1
		}
	}
	c.entries = kept
	if hit == 0 {
		return Identity{}, false, false
	}
	return found.identity, now.Sub(found.verified) <= c.ttl, true
}

func (c *tokenCache) store(token string, identity Identity) {
	digest := sha256.Sum256([]byte(token))
	now := c.now()

	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.entries {
		if subtle.ConstantTimeCompare(digest[:], c.entries[i].digest[:]) == 1 {
			c.entries[i].identity = identity
			c.entries[i].verified = now
			return
		}
	}
	c.entries = append(c.entries, cacheEntry{digest: digest, identity: identity, verified: now})
}

// forget drops a token the plane has refused, so a revoked credential does not
// keep working out of the grace window.
func (c *tokenCache) forget(token string) {
	digest := sha256.Sum256([]byte(token))

	c.mu.Lock()
	defer c.mu.Unlock()
	kept := c.entries[:0]
	for _, entry := range c.entries {
		if subtle.ConstantTimeCompare(digest[:], entry.digest[:]) == 1 {
			continue
		}
		kept = append(kept, entry)
	}
	c.entries = kept
}

// forgetCluster drops every entry that resolved to clusterID.
func (c *tokenCache) forgetCluster(clusterID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	kept := c.entries[:0]
	for _, entry := range c.entries {
		if entry.identity.ClusterID == clusterID {
			continue
		}
		kept = append(kept, entry)
	}
	c.entries = kept
}
