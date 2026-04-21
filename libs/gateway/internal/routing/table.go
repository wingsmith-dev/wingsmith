// Package routing holds the routing table in memory and picks an upstream for
// one request.
//
// The plane derives the table; this package is the kube-proxy to its
// EndpointSlice. It is refreshed on a timer and never joined on the request
// path — a completion is a long, expensive call and the cheapest thing on it
// should be deciding where to send it.
//
// A failed refresh keeps the table that is already loaded. A control-plane
// outage must not take down traffic to models that are running perfectly well,
// and the data the table is built from changes on the order of days.
package routing

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// ErrUnknownEndpoint is returned when no endpoint answers to a name.
var ErrUnknownEndpoint = errors.New("unknown endpoint")

// ErrNoUpstream is returned when an endpoint exists and has nowhere to send
// traffic. Distinct from ErrUnknownEndpoint because the caller named something
// real: it is capacity, not a bad request.
var ErrNoUpstream = errors.New("no upstream available")

// Upstream is one address behind an endpoint, with its proxy already built.
type Upstream struct {
	DeploymentID string
	ClusterID    string
	ClusterName  string
	URL          string
	Weight       int
	Role         string
	// Ready is the plane's observation of the deployment, not a probe of our
	// own. It orders candidates; it never removes the last one.
	Ready bool

	handler http.Handler
}

// Handler returns the reverse proxy forwarding to this upstream.
func (u Upstream) Handler() http.Handler { return u.handler }

// Route is one public name and the upstreams behind it.
type Route struct {
	Name      string
	Strategy  string
	Upstreams []Upstream

	totalWeight int
	cursor      *atomic.Uint64
}

// NewUpstream binds an address to the handler that forwards to it.
//
// Load builds these from the plane's table; this exists for the cases that
// build one directly — tests, and anything embedding the router.
func NewUpstream(clusterID string, weight int, ready bool, handler http.Handler) Upstream {
	return Upstream{ClusterID: clusterID, Weight: weight, Ready: ready, handler: handler}
}

// NewRoute assembles a route from upstreams that are already bound.
func NewRoute(name, strategy string, upstreams ...Upstream) Route {
	route := Route{Name: name, Strategy: strategy, Upstreams: upstreams, cursor: &atomic.Uint64{}}
	for _, upstream := range upstreams {
		route.totalWeight += upstream.Weight
	}
	return route
}

// Pick chooses one upstream for one request.
//
// Ready upstreams are preferred and unready ones are a fallback rather than an
// exclusion: readiness is observed, it can lag by a sync interval, and refusing
// to send traffic to a model that is in fact serving is a worse failure than
// sending it to one that is not.
//
// Weighted picks by round-robin rather than at random, so a weight of 5 out of
// 100 is honoured exactly rather than on average — which is the whole point
// when a cluster is being ramped up. Failover always picks the first live
// upstream in the declared order.
func (r Route) Pick() (Upstream, error) {
	candidates := r.Upstreams
	if ready := filterReady(candidates); len(ready) > 0 {
		candidates = ready
	}

	switch len(candidates) {
	case 0:
		return Upstream{}, ErrNoUpstream
	case 1:
		return candidates[0], nil
	}

	if r.Strategy == "failover" {
		return candidates[0], nil
	}

	total := 0
	for _, upstream := range candidates {
		total += upstream.Weight
	}
	if total <= 0 {
		return candidates[0], nil
	}

	target := (r.cursor.Add(1) - 1) % uint64(total)
	var acc uint64
	for _, upstream := range candidates {
		acc += uint64(upstream.Weight)
		if target < acc {
			return upstream, nil
		}
	}
	return candidates[len(candidates)-1], nil
}

func filterReady(upstreams []Upstream) []Upstream {
	ready := make([]Upstream, 0, len(upstreams))
	for _, upstream := range upstreams {
		if upstream.Ready {
			ready = append(ready, upstream)
		}
	}
	return ready
}

// Source supplies the table. ErrNotModified means the revision still holds.
type Source interface {
	Resolved(ctx context.Context, revision string) (plane.Table, error)
}

// ProxyFactory builds the handler that forwards to one upstream, given the
// credential reference the plane attached to its address.
type ProxyFactory func(upstream *url.URL, credentialID string) http.Handler

// Router holds the current table.
type Router struct {
	source  Source
	factory ProxyFactory

	mu       sync.RWMutex
	revision string
	routes   map[string]Route
	// proxies is keyed by address and credential together, so upstreams sharing
	// both share a connection pool and one presenting a different credential
	// never borrows another's.
	proxies map[string]http.Handler
}

// New builds an empty Router. Load must succeed once before it can serve.
func New(source Source, factory ProxyFactory) *Router {
	return &Router{
		source:  source,
		factory: factory,
		routes:  make(map[string]Route),
		proxies: make(map[string]http.Handler),
	}
}

// Load refreshes the table, or leaves it alone when the plane reports the
// current revision still holds.
func (r *Router) Load(ctx context.Context) error {
	r.mu.RLock()
	revision := r.revision
	previous := r.routes
	pools := make(map[string]http.Handler, len(r.proxies))
	for key, handler := range r.proxies {
		pools[key] = handler
	}
	r.mu.RUnlock()

	table, err := r.source.Resolved(ctx, revision)
	if errors.Is(err, plane.ErrNotModified) {
		return nil
	}
	if err != nil {
		return err
	}

	next := make(map[string]Route, len(table.Endpoints))
	used := make(map[string]bool, len(pools))

	for _, endpoint := range table.Endpoints {
		route := Route{
			Name:      endpoint.Name,
			Strategy:  endpoint.RoutingStrategy,
			Upstreams: make([]Upstream, 0, len(endpoint.Upstreams)),
		}

		for _, upstream := range order(endpoint) {
			parsed, err := url.Parse(upstream.URL)
			if err != nil || parsed.Scheme == "" || parsed.Host == "" {
				// Skip the address rather than fail the refresh: one bad
				// serving URL must not take every other model down with it.
				slog.Error("gateway: skipping upstream with unusable address",
					"endpoint", endpoint.Name, "cluster", upstream.ClusterName, "url", upstream.URL, "err", err)
				continue
			}

			key := upstream.URL + "\x00" + upstream.CredentialID
			handler, ok := pools[key]
			if !ok {
				handler = r.factory(parsed, upstream.CredentialID)
				pools[key] = handler
			}
			used[key] = true

			weight := upstream.Weight
			if weight < 1 {
				weight = 1
			}
			route.Upstreams = append(route.Upstreams, Upstream{
				DeploymentID: upstream.DeploymentID,
				ClusterID:    upstream.ClusterID,
				ClusterName:  upstream.ClusterName,
				URL:          upstream.URL,
				Weight:       weight,
				Role:         upstream.Role,
				Ready:        upstream.AdmissionPhase == "running",
				handler:      handler,
			})
			route.totalWeight += weight
		}

		// Carried across refreshes so a resync does not restart every route's
		// rotation at the same upstream.
		if prior, ok := previous[route.Name]; ok && prior.cursor != nil {
			route.cursor = prior.cursor
		} else {
			route.cursor = &atomic.Uint64{}
		}
		next[route.Name] = route
	}

	for key := range pools {
		if !used[key] {
			delete(pools, key)
		}
	}

	r.mu.Lock()
	r.revision = table.Revision
	r.routes = next
	r.proxies = pools
	r.mu.Unlock()

	slog.Info("gateway: routing table loaded",
		"revision", table.Revision, "endpoints", len(next), "upstreams", len(pools))
	return nil
}

// order sorts an endpoint's upstreams by its declared failover order, so a
// failover route can pick the head of the list and a weighted one is stable
// across refreshes.
func order(endpoint plane.Endpoint) []plane.Upstream {
	rank := make(map[string]int, len(endpoint.FailoverOrder))
	for i, deployment := range endpoint.FailoverOrder {
		rank[deployment] = i
	}
	sorted := make([]plane.Upstream, len(endpoint.Upstreams))
	copy(sorted, endpoint.Upstreams)
	sort.SliceStable(sorted, func(i, j int) bool {
		left, leftRanked := rank[sorted[i].DeploymentID]
		right, rightRanked := rank[sorted[j].DeploymentID]
		if leftRanked != rightRanked {
			return leftRanked
		}
		if leftRanked && left != right {
			return left < right
		}
		return sorted[i].DeploymentID < sorted[j].DeploymentID
	})
	return sorted
}

// Resolve returns the route answering to a name.
func (r *Router) Resolve(name string) (Route, error) {
	r.mu.RLock()
	route, ok := r.routes[name]
	r.mu.RUnlock()
	if !ok {
		return Route{}, ErrUnknownEndpoint
	}
	return route, nil
}

// Names lists what this gateway currently serves.
func (r *Router) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.routes))
	for name := range r.routes {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// Revision reports the table currently loaded.
func (r *Router) Revision() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.revision
}

// RunResync refreshes the table until the context is cancelled.
func (r *Router) RunResync(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := r.Load(ctx); err != nil {
				slog.Error("gateway: routing table refresh failed, serving the table already loaded", "err", err)
			}
		}
	}
}
