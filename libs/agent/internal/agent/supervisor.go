package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	mrand "math/rand"
	"net"
	"sort"
	"sync"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// DialFunc opens an authenticated tunnel to the gateway and reports which replica answered.
// covered names the replicas already at quota, so one of them can answer tunnel.ErrDuplicate with
// its Peer instead of accepting a tunnel that would be closed. Dialler.Dial is one; a test
// supplies its own.
type DialFunc func(ctx context.Context, target string, covered []string) (net.Conn, Peer, error)

// Server is the HTTP/2 end that runs on a dialled connection. tunnel.Connector is the one there
// is; the interface exists so this package does not have to build one to be tested.
type Server interface {
	// Serve runs one tunnel and blocks until it ends. It always closes the connection.
	Serve(conn net.Conn) error
}

// SupervisorConfig configures the reconnect loops.
type SupervisorConfig struct {
	// Gateway is the one address to dial, as host:port. Not a list: how many replicas are behind
	// it is the gateway's business to report, not this cluster's to be told.
	Gateway string
	// ConnsPerGateway is how many tunnels each replica gets. Zero means one.
	ConnsPerGateway int
	// Dial opens a tunnel. Required.
	Dial DialFunc
	// Server serves HTTP/2 on a dialled tunnel. Required.
	Server Server
	// BackoffMin and BackoffMax bound the reconnect backoff.
	BackoffMin time.Duration
	BackoffMax time.Duration
	// Metrics, when set, records connection state per replica.
	Metrics *Metrics
	// Log is where reconnects are reported. Nil means slog.Default.
	Log *slog.Logger
}

// Supervisor holds tunnels to every gateway replica, having been told only one address.
//
// Coverage has to be complete: the tunnel pool is in-process, so a gateway replica can only serve a
// cluster it is personally holding a tunnel to, and one that holds none answers nothing for that
// cluster while looking healthy from every other vantage point. So this cluster needs a tunnel to
// each replica, but it must not be handed a list of them, because then scaling the gateway would
// mean editing the values of every cluster in the fleet.
//
// So it redials the one address until every replica has answered. Each accepted handshake names the
// replica that took it and says how many there are. Every dial lists the replicas already at quota,
// and one of those that answers says so instead of accepting; that is retried, and is ordinary and
// not a failure. That is the pattern Konnectivity,
// Teleport and ngrok each arrived at independently, and the reason it is the handshake that carries
// the number instead of a call to the plane is that a cluster must be able to recover its own data
// path while the control plane is down.
//
// There is no periodic re-probe of the count, and there is a reason it is not needed here.
// HANGAR_GATEWAY_REPLICAS lives in the gateway's pod spec, so changing it necessarily restarts every
// replica, which severs every tunnel and makes every agent redial and read the new number. The one
// way to defeat that is to scale the StatefulSet without editing the env, and the replica that
// results holds no tunnels, so it reports itself unready, never joins the Service, and stalls a
// rollout that somebody is watching. Loud, and not silently wrong.
type Supervisor struct {
	cfg SupervisorConfig
	log *slog.Logger

	// resize wakes Run when a handshake reported more replicas than there are workers for.
	// Buffered and sent to without blocking: it is a nudge, and a nudge already pending is the
	// same nudge.
	resize chan struct{}

	mu sync.Mutex
	// live is how many tunnels each replica has, keyed by the identity it reported. An entry at
	// zero is a replica that has been reached and is not reachable now, which is the distinction
	// worth keeping on a readiness page.
	live map[string]int
	// desired is how many replicas the gateway last said it was running.
	desired int
	// workers is how many hold loops are running.
	workers int
}

// NewSupervisor builds the supervisor.
func NewSupervisor(cfg SupervisorConfig) (*Supervisor, error) {
	if cfg.Gateway == "" {
		return nil, errors.New("agent: no gateway to dial")
	}
	if cfg.Dial == nil {
		return nil, errors.New("agent: supervisor needs a dialler")
	}
	if cfg.Server == nil {
		return nil, errors.New("agent: supervisor needs a server")
	}
	if cfg.ConnsPerGateway < 1 {
		cfg.ConnsPerGateway = 1
	}
	if cfg.BackoffMin <= 0 {
		cfg.BackoffMin = time.Second
	}
	if cfg.BackoffMax < cfg.BackoffMin {
		cfg.BackoffMax = cfg.BackoffMin
	}
	log := cfg.Log
	if log == nil {
		log = slog.Default()
	}

	// One replica until told otherwise. Starting at zero would mean starting no workers, and
	// nothing would ever dial to learn the real number.
	return &Supervisor{
		cfg:     cfg,
		log:     log,
		resize:  make(chan struct{}, 1),
		live:    make(map[string]int),
		desired: 1,
	}, nil
}

// Run holds the tunnels up until ctx is cancelled, then returns once every worker has let go.
//
// Cancelling ctx stops dialling. It does not drain: the tunnels themselves are drained by the
// server's own Shutdown, which is what sends GOAWAY and lets Serve return. A caller that cancels
// without shutting the server down severs whatever was in flight.
func (s *Supervisor) Run(ctx context.Context) {
	var wg sync.WaitGroup
	s.spawn(ctx, &wg)

	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case <-s.resize:
			s.spawn(ctx, &wg)
		}
	}
}

// spawn starts however many hold loops the current replica count calls for.
//
// It only ever adds. Retiring is the workers' own job, see retire, because a worker that is
// mid-tunnel must not be stopped by an arithmetic change, and a slot freed by killing a goroutine
// that was carrying requests is not a saving.
func (s *Supervisor) spawn(ctx context.Context, wg *sync.WaitGroup) {
	s.mu.Lock()
	want := s.desired * s.cfg.ConnsPerGateway
	start := want - s.workers
	if start > 0 {
		s.workers = want
	}
	s.mu.Unlock()

	for i := 0; i < start; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.hold(ctx)
		}()
	}
}

// hold keeps one tunnel up, to whichever replica it lands on.
func (s *Supervisor) hold(ctx context.Context) {
	attempt := 0
	// The wait is drawn once and both slept on and logged, so that what the log says it is about
	// to do is what it does.
	var wait time.Duration
	for {
		if !sleep(ctx, wait) {
			return
		}
		if s.retire() {
			return
		}

		if s.cfg.Metrics != nil {
			s.cfg.Metrics.dialled()
		}
		conn, peer, err := s.cfg.Dial(ctx, s.cfg.Gateway, s.covered())
		if errors.Is(err, tunnel.ErrDuplicate) {
			s.observe(peer)
			attempt++
			wait = s.backoff(attempt)
			s.landedOnCovered(peer.ID, wait)
			continue
		}
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			attempt++
			wait = s.backoff(attempt)
			if s.cfg.Metrics != nil {
				s.cfg.Metrics.dialFailed(classifyDial(err))
			}
			// Warn and not Error: one unreachable replica out of several is a gap in
			// coverage, not an outage, and the readiness gate is what escalates the case where
			// they are all unreachable.
			s.log.Warn("agent: tunnel dial failed", "attempt", attempt, "retry_in", wait, "err", err)
			continue
		}

		// Learned before the claim, and from every accepted handshake including the ones whose
		// connection is about to be thrown away. The count is the point of the dial as much as the
		// tunnel is.
		s.observe(peer)

		claimed, forgotten := s.claim(peer.ID)
		s.forget(forgotten)
		if !claimed {
			// Another worker filled this replica's quota while this dial was in flight, so the
			// covered list it carried was out of date. Same ending as a duplicate the gateway
			// answered.
			_ = conn.Close()
			attempt++
			wait = s.backoff(attempt)
			s.landedOnCovered(peer.ID, wait)
			continue
		}

		wait = 0
		opened := time.Now()
		live := s.opened(peer.ID)
		log := s.log.With("gateway", peer.ID)
		log.Info("agent: tunnel established", "local", localAddr(conn), "live", live, "replicas", peer.Total)

		err = s.cfg.Server.Serve(conn)
		lasted := time.Since(opened)
		s.closed(peer.ID, lasted)

		if errors.Is(err, tunnel.ErrConnectorClosed) {
			log.Info("agent: shutting down, not redialling")
			return
		}
		if ctx.Err() != nil {
			return
		}

		// A tunnel that lived a while and then ended is an ordinary rollout of the gateway, and
		// redialling immediately is what keeps coverage. A tunnel that died at once is a replica
		// that accepts connections and cannot serve them, and hammering it would make
		// the accept queue the symptom instead of the cause, so the backoff keeps climbing.
		if lasted >= s.cfg.BackoffMax {
			attempt = 0
		} else {
			attempt++
		}
		wait = s.backoff(attempt)
		log.Info("agent: tunnel closed", "lasted", lasted.Round(time.Millisecond), "retry_in", wait, "err", err)
	}
}

// landedOnCovered records a dial that reached a replica already at quota. The load balancer picks
// without knowing what this agent holds, so this is ordinary: counted apart from failures, and
// retried with a backoff that climbs only so a stale replica count cannot become a hot loop.
func (s *Supervisor) landedOnCovered(id string, wait time.Duration) {
	if s.cfg.Metrics != nil {
		s.cfg.Metrics.duplicate(id)
	}
	s.log.Debug("agent: dial landed on a replica already covered", "gateway", id, "retry_in", wait)
}

// covered lists the replicas that already have their quota of tunnels from this agent.
func (s *Supervisor) covered() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var ids []string
	for id, live := range s.live {
		if live >= s.cfg.ConnsPerGateway {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}

// retire reports whether this worker is surplus and should stop.
//
// Checked between tunnels and never during one. A gateway that has scaled down leaves workers with
// nowhere to land, and they would otherwise redial forever collecting duplicates.
func (s *Supervisor) retire() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.workers <= s.desired*s.cfg.ConnsPerGateway {
		return false
	}
	s.workers--
	return true
}

// maxGatewayReplicas caps the replica count a handshake can ask this agent to cover. Each replica
// costs ConnsPerGateway goroutines and sockets, and the number arrives from the far end of a network
// connection; a gateway that reports thousands is misconfigured, and following it would exhaust
// this pod, not cover anything.
const maxGatewayReplicas = 64

// observe records what a handshake said about the size of the gateway.
//
// The number is taken from the latest accepted handshake and is not held at a high-water mark. A
// monotonic count would never notice a gateway that scaled down, and would leave workers redialling
// after a replica that no longer exists; taking the latest instead is safe here because the count
// only changes by restarting the replicas, so the last handshake is always with a replica that
// survived the change.
func (s *Supervisor) observe(peer Peer) {
	if peer.Total > maxGatewayReplicas {
		s.log.Warn("agent: gateway reported more replicas than this agent will cover, clamping",
			"gateway", peer.ID, "reported", peer.Total, "covering", maxGatewayReplicas)
		peer.Total = maxGatewayReplicas
	}
	s.mu.Lock()
	previous := s.desired
	s.desired = peer.Total
	grew := s.desired*s.cfg.ConnsPerGateway > s.workers
	forgotten := s.prune()
	s.mu.Unlock()

	if previous != peer.Total {
		s.log.Info("agent: gateway replica count changed",
			"gateway", peer.ID, "was", previous, "now", peer.Total)
	}
	if s.cfg.Metrics != nil {
		s.cfg.Metrics.expect(peer.Total)
	}
	s.forget(forgotten)
	if grew {
		select {
		case s.resize <- struct{}{}:
		default:
		}
	}
}

// prune drops the replicas the gateway has shrunk past, and names them so their series can go too.
//
// Only ones holding no tunnel: a replica still carrying requests is not gone whatever the arithmetic
// says. It has to run after every change to live and not only after a count changes, because
// mid-rollout there is a moment when several entries are at zero and only some of them are on the way
// out: pruning then would pick by map order, and pruning again once the survivors have their tunnels
// back is what makes the choice correctly instead of by luck.
//
// The caller holds the lock.
func (s *Supervisor) prune() []string {
	var forgotten []string
	for id, live := range s.live {
		if len(s.live) <= s.desired {
			break
		}
		if live == 0 {
			delete(s.live, id)
			forgotten = append(forgotten, id)
		}
	}
	return forgotten
}

// claim reserves a slot on one replica, or reports that it already has its quota.
func (s *Supervisor) claim(id string) (bool, []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.live[id] >= s.cfg.ConnsPerGateway {
		return false, nil
	}
	// Zero, not absent from here on, so a replica that has been reached and lost reads as a
	// gap and not as one nobody ever heard of.
	if _, seen := s.live[id]; !seen {
		s.live[id] = 0
		if s.cfg.Metrics != nil {
			s.cfg.Metrics.track(id)
		}
	}
	s.live[id]++
	return true, s.prune()
}

// forget drops the series for replicas that have been pruned.
func (s *Supervisor) forget(ids []string) {
	if s.cfg.Metrics == nil {
		return
	}
	for _, id := range ids {
		s.cfg.Metrics.forget(id)
	}
}

// backoff is exponential from BackoffMin to BackoffMax with full jitter, matching the operator's
// hub reconnect. Attempt zero is immediate: the first dial, and the redial after a tunnel that had
// been carrying traffic, should not wait.
//
// Full jitter and not a fixed schedule, because every agent in the fleet redials at once when the
// gateway is rolled: that burst is the accepted cost of holding a tunnel to every replica, and
// jitter is what keeps it a burst instead of a synchronised wave that repeats.
func (s *Supervisor) backoff(attempt int) time.Duration {
	if attempt <= 0 {
		return 0
	}
	d := float64(s.cfg.BackoffMin) * math.Pow(2, float64(attempt-1))
	if d > float64(s.cfg.BackoffMax) || math.IsInf(d, 0) {
		d = float64(s.cfg.BackoffMax)
	}
	return time.Duration(mrand.Float64() * d)
}

// sleep waits, and reports whether the caller should carry on.
func sleep(ctx context.Context, d time.Duration) bool {
	if ctx.Err() != nil {
		return false
	}
	if d <= 0 {
		return true
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (s *Supervisor) opened(id string) int {
	s.mu.Lock()
	live := s.live[id]
	s.mu.Unlock()
	if s.cfg.Metrics != nil {
		s.cfg.Metrics.tunnelOpened(id, live)
	}
	return live
}

// closed releases the slot, and forgets the replica entirely once the gateway has shrunk past it.
//
// Forgetting is what keeps a scale-down from leaving a permanent zero behind: both on the
// readiness page and, through Metrics.forget, in a series that would otherwise alert forever about a
// replica that no longer exists.
func (s *Supervisor) closed(id string, lasted time.Duration) {
	s.mu.Lock()
	if s.live[id] > 0 {
		s.live[id]--
	}
	live := s.live[id]
	forgotten := s.prune()
	s.mu.Unlock()

	// Reported before anything is forgotten, so the last close of a replica on its way out is still
	// counted, not written to a series that has just been deleted.
	if s.cfg.Metrics != nil {
		s.cfg.Metrics.tunnelClosed(id, live, lasted.Seconds())
	}
	s.forget(forgotten)
}

// Coverage is how many tunnels each replica this agent has reached currently has, including the ones
// at zero.
func (s *Supervisor) Coverage() map[string]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	coverage := make(map[string]int, len(s.live))
	for id, live := range s.live {
		coverage[id] = live
	}
	return coverage
}

// Expected is how many replicas the gateway last reported.
func (s *Supervisor) Expected() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.desired
}

// Tunnels is how many tunnels are established in total.
func (s *Supervisor) Tunnels() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	total := 0
	for _, live := range s.live {
		total += live
	}
	return total
}

// Ready reports whether this agent can carry a request.
//
// One tunnel is enough, and the bar is not full coverage. An agent that refused to
// be ready until every gateway replica had answered would fail its rollout because one replica was
// restarting, and it can serve traffic perfectly well down the tunnels it does have. The replicas
// it is missing are visible as metrics, which is the right place for a partial answer.
func (s *Supervisor) Ready() error {
	s.mu.Lock()
	total := 0
	missing := make([]string, 0, len(s.live))
	for id, live := range s.live {
		total += live
		if live == 0 {
			missing = append(missing, id)
		}
	}
	desired, reached := s.desired, len(s.live)
	s.mu.Unlock()

	if total > 0 {
		return nil
	}
	if reached == 0 {
		return fmt.Errorf("no tunnel to %s, and no gateway replica has answered yet", s.cfg.Gateway)
	}
	sort.Strings(missing)
	return fmt.Errorf("no tunnel to any of the %d gateway replicas reached so far of %d (%v)",
		reached, desired, missing)
}

// classifyDial buckets a dial failure for the metric. A rejected token is the one worth separating:
// it does not heal on its own, and it is the difference between "the gateway is not there" and "the
// gateway does not believe this cluster is who it says it is". A gateway that could not reach the
// plane to check the token is not that, and is counted as refused.
func classifyDial(err error) string {
	switch {
	case errors.Is(err, tunnel.ErrRejected):
		return reasonUnauthorized
	case errors.Is(err, tunnel.ErrRefused):
		return reasonRefused
	case errors.Is(err, tunnel.ErrHandshake):
		return reasonHandshake
	default:
		return reasonDial
	}
}

func localAddr(conn net.Conn) string {
	if addr := conn.LocalAddr(); addr != nil {
		return addr.String()
	}
	return "unknown"
}
