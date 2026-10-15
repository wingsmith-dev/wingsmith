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
	"strings"
	"sync"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// supportedPolicyVersion is the highest endpoint policy version this build
// reads. It is duplicated from serving deliberately: routing reports what it
// cannot enforce, serving enforces it, and neither should import the other.
//
// 2 is the scoped-rule shape. 1 was a map of parameter names to constraints
// plus a list of denial names, and this build no longer reads it — the plane
// converts a stored v1 policy on the way out, so nothing on this side has to
// carry two languages.
const supportedPolicyVersion = 2

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
	// Tunnelled means this upstream has no dialable address and is reachable
	// only over a tunnel the cluster dialled out. It is the normal case: no
	// cluster publishes a serving URL, so the URL above is usually empty and the
	// cluster id is the whole of the routing key.
	Tunnelled bool
	// ServedModelName is what the engine behind this upstream answers to, which
	// is often not the endpoint the caller named: the plane resolves it to the
	// workload's own declaration, or to the artifact's lowercase Hugging Face
	// slug when it declares none. Requests are rewritten to it on the way
	// through. Empty means the plane had nothing to say, and then the caller's
	// own string is forwarded untouched.
	//
	// It used to be a fit's name — `llama-3-3-70b-instruct-fp8-tp2-32k` — which
	// put the hardware footprint on the public API surface, so re-fitting a model
	// at a different tensor degree renamed the model every client codes against.
	ServedModelName string
	// OutputModality is what this upstream's engine emits — `text`, `image` or `video` — and
	// empty for text. It decides which paths the upstream answers; see `serving.pathAnswered`.
	OutputModality string

	handler http.Handler
	// reachable answers whether this upstream can be used right now. Nil means
	// yes, which is what an upstream with a dialable address is.
	reachable func() bool
}

// Reachable reports whether this upstream can take a request at this instant.
//
// It is deliberately not readiness. Readiness is the plane's observation of a
// deployment and lags by a sync interval; this is a local fact about this
// replica — whether there is a live tunnel to the cluster, or an address to dial
// instead. An upstream that is not reachable is not a candidate at all, which is
// what keeps a cluster with neither a tunnel nor an address answering the
// no-capacity 503 rather than a 502 from a proxy that had nowhere to go.
func (u Upstream) Reachable() bool {
	return u.reachable == nil || u.reachable()
}

// Handler returns the reverse proxy forwarding to this upstream.
func (u Upstream) Handler() http.Handler { return u.handler }

// Route is one public name and the upstreams behind it.
type Route struct {
	// ID is the endpoint's uid. It is carried for one reason: a metric
	// labelled by a public model name is joined against a plane object that
	// renames freely, and a rename would silently split a series in two.
	ID string
	// Name is the endpoint's canonical model id, and the only one anything
	// downstream ever sees. Usage, policy, metrics and logs are keyed on it,
	// which is what makes a rename cheap: the old id becomes an alias and no
	// record is renamed.
	Name string
	// Aliases are the other keys this route is indexed under. They are here to
	// be reported, not to be resolved through — resolution is one map lookup
	// over every key alike.
	Aliases   []string
	Strategy  string
	Upstreams []Upstream
	// Policy is the request terms this endpoint sells, or nil for the ordinary
	// endpoint that sells none. It rides the table rather than being fetched per
	// request for the same reason everything else here does: the table refreshes
	// on a timer and is never joined on the request path.
	Policy *plane.Policy
	// TenantID is the endpoint's project's tenant (docs/decisions/0016), carried the
	// same way Policy is — read on every request and refreshed on a timer,
	// never fetched per request.
	TenantID string
	// Visibility is the endpoint's "public" or "private". Only a public
	// endpoint is served on the public listener; a private one, the plane's
	// default, answers on the internal listener alone.
	Visibility string

	totalWeight int
	cursor      *picker
}

// picker holds a weighted route's rotation state across requests.
//
// Shared by pointer so that copying a Route — which happens on every table read —
// keeps one rotation rather than restarting it, and carried across refreshes for
// the same reason.
type picker struct {
	mu sync.Mutex
	// current is the running credit per upstream, keyed by an identity that
	// survives a refresh. A key whose upstream is momentarily not a candidate
	// keeps its credit rather than losing it, so a replica that flaps back into
	// readiness does not have to earn its place from zero.
	current map[string]int
}

func newPicker() *picker { return &picker{current: map[string]int{}} }

// pickKey identifies an upstream within its route across refreshes. The
// deployment is the real identity; `NewUpstream` leaves it empty, so the cluster
// answers for upstreams built by hand.
func pickKey(u Upstream) string {
	if u.DeploymentID != "" {
		return u.DeploymentID
	}
	return u.ClusterID
}

// next chooses among candidates by smooth weighted round-robin — nginx's
// algorithm — and it is chosen over walking a cursor across the cumulative
// weight range because the two agree on the totals and disagree on the order,
// which is the part that matters.
//
// A cursor over cumulative ranges hands out each upstream's whole share in one
// contiguous run: two upstreams at the default weight of 100 mean a hundred
// consecutive requests to the first cluster while the second sits idle, and any
// burst shorter than the cycle never reaches the second at all. Worse where it
// was supposed to help most — a canary at 5 against 100 got its five as five
// consecutive requests rather than as one in every twenty-one, so the ramp
// measured a five-request spike instead of a sampled share of live traffic.
//
// Smooth WRR spends the same budget per cycle and spreads it: 100/100
// alternates, and 5/100 puts the five where a sample belongs. Ties go to the
// earlier upstream so the choice stays deterministic for a given state.
func (p *picker) next(candidates []Upstream, total int) Upstream {
	p.mu.Lock()
	defer p.mu.Unlock()

	best := 0
	for i, candidate := range candidates {
		key := pickKey(candidate)
		p.current[key] += candidate.Weight
		if p.current[key] > p.current[pickKey(candidates[best])] {
			best = i
		}
	}
	p.current[pickKey(candidates[best])] -= total
	return candidates[best]
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
	route := Route{Name: name, Strategy: strategy, Upstreams: upstreams, cursor: newPicker()}
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
// 100 is honoured exactly rather than on average — which is the whole point when
// a cluster is being ramped up — and it is spread across the cycle rather than
// spent in one run, which is the point of `picker.next`. Failover always picks
// the first live upstream in the declared order.
func (r Route) Pick() (Upstream, error) {
	candidates := filterReachable(r.Upstreams)
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

	return r.cursor.next(candidates, total), nil
}

// filterReachable drops the upstreams that have nowhere to send a request. It
// runs before the readiness preference, because an unreachable upstream is not a
// worse candidate — it is not a candidate.
func filterReachable(upstreams []Upstream) []Upstream {
	reachable := make([]Upstream, 0, len(upstreams))
	for _, upstream := range upstreams {
		if upstream.Reachable() {
			reachable = append(reachable, upstream)
		}
	}
	return reachable
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

// Target is where one upstream's traffic goes.
//
// It carries a cluster id as well as an address because the address is usually
// absent: a cluster is reached over the tunnel it dialled out, and the cluster id
// is what the tunnel pool is keyed by. The URL path is kept alongside it because
// ClusterServing still exists and is the fallback — both modes coexist, and a
// cluster that publishes a serving address is dialled at it.
type Target struct {
	// ClusterID is the cluster this upstream is in. Always set.
	ClusterID string
	// ClusterName is for logs and metrics, never for routing.
	ClusterName string
	// URL is the cluster's published serving address, nil when it has none.
	URL *url.URL
	// CredentialID is the credential the plane attached to the address.
	CredentialID string
}

// ProxyFactory builds the handler that forwards to one upstream.
type ProxyFactory func(target Target) http.Handler

// Reachability reports whether a cluster can be reached from this replica right
// now. The tunnel pool implements it.
//
// It is read on the request path, so it must be cheap and must not block. It is
// never written to: reachability is a fact about this process, not observed state
// — that has exactly one writer and it is the plane's ingestion.
type Reachability interface {
	Live(clusterID string) int
}

// Observer is told what each refresh did, so the health of the table this hop
// serves from is visible without asking the plane — which is the one thing that
// cannot be asked when the plane is the thing that is down.
type Observer interface {
	TableApplied(endpoints int, upstreams []metrics.UpstreamSample)
	TableUnchanged()
	TableRefreshFailed()
}

// Option adjusts a Router at construction.
type Option func(*Router)

// WithObserver attaches a refresh observer. Absent, refreshes are unobserved
// and everything else behaves identically.
func WithObserver(observer Observer) Option {
	return func(r *Router) { r.observer = observer }
}

// WithReachability attaches the tunnel pool. Absent, an upstream with no
// dialable address has nowhere to go and is never picked.
func WithReachability(tunnels Reachability) Option {
	return func(r *Router) { r.tunnels = tunnels }
}

// Router holds the current table.
type Router struct {
	source   Source
	factory  ProxyFactory
	observer Observer
	tunnels  Reachability

	mu       sync.RWMutex
	revision string
	// routes is keyed by every name that reaches a route — the canonical and
	// each of its aliases, all lowercased — so resolving an alias costs the
	// same one lookup as resolving the canonical.
	routes map[string]Route
	// canonicals lists the routes' own names, sorted, and is what the catalog
	// is answered from. An alias in `GET /v1/models` would show one model
	// several times in a client's picker.
	canonicals []string
	// proxies is keyed by address and credential together, so upstreams sharing
	// both share a connection pool and one presenting a different credential
	// never borrows another's.
	proxies map[string]http.Handler
}

// New builds an empty Router. Load must succeed once before it can serve.
func New(source Source, factory ProxyFactory, opts ...Option) *Router {
	router := &Router{
		source:  source,
		factory: factory,
		routes:  make(map[string]Route),
		proxies: make(map[string]http.Handler),
	}
	for _, opt := range opts {
		opt(router)
	}
	return router
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
		// The plane vouched for what is already loaded, which is as good a
		// confirmation as a new table and is reported as one.
		r.observe(func(o Observer) { o.TableUnchanged() })
		return nil
	}
	if err != nil {
		r.observe(func(o Observer) { o.TableRefreshFailed() })
		return err
	}

	next := make(map[string]Route, len(table.Endpoints))
	built := make([]Route, 0, len(table.Endpoints))
	used := make(map[string]bool, len(pools))

	for _, endpoint := range table.Endpoints {
		route := Route{
			ID:         endpoint.ID,
			Name:       endpoint.Name,
			Aliases:    endpoint.Aliases,
			Strategy:   endpoint.RoutingStrategy,
			Policy:     endpoint.Policy,
			TenantID:   endpoint.TenantID,
			Visibility: endpoint.Visibility,
			Upstreams:  make([]Upstream, 0, len(endpoint.Upstreams)),
		}

		// Reported here rather than per request. A policy written against a
		// newer plane is enforced as far as this build reads it, and the part it
		// cannot read is a conformance gap somebody has to see — once a refresh,
		// not once a completion.
		if p := endpoint.Policy; p != nil {
			switch {
			case p.Version > supportedPolicyVersion:
				slog.Warn("gateway: endpoint policy is newer than this build",
					"endpoint", endpoint.Name, "policy_version", p.Version, "supported", supportedPolicyVersion)
			case len(p.Rules) == 0:
				// A policy that carries no rules enforces nothing, and an
				// endpoint that sold nothing sends no policy at all — so this
				// is either an empty one somebody left behind or a plane still
				// serving the v1 shape this build stopped reading. Both are a
				// term that is not being honoured, which is the one thing a
				// consistency policy must never be silent about.
				slog.Warn("gateway: endpoint policy carries no rules this build can read",
					"endpoint", endpoint.Name, "policy_version", p.Version)
			}
		}

		for _, upstream := range order(endpoint) {
			if upstream.ClusterID == "" {
				// Nothing can be done with this one: with no address and no
				// cluster there is no tunnel to look up either. Skipped rather
				// than failing the refresh, so one bad row does not take every
				// other model down with it.
				slog.Error("gateway: skipping upstream with no cluster",
					"endpoint", endpoint.Name, "deployment", upstream.DeploymentID)
				continue
			}

			// An address is optional now and usually absent. A cluster is
			// reached over the tunnel it dialled out; a published serving URL is
			// the fallback that ClusterServing still provides, and an
			// unparseable one is treated as absent rather than as a reason to
			// drop the cluster.
			var address *url.URL
			if upstream.URL != "" {
				parsed, err := url.Parse(upstream.URL)
				switch {
				case err != nil || parsed.Scheme == "" || parsed.Host == "":
					slog.Error("gateway: ignoring unusable serving address, this cluster is tunnel-only",
						"endpoint", endpoint.Name, "cluster", upstream.ClusterName, "url", upstream.URL, "err", err)
				default:
					address = parsed
				}
			}

			// Keyed by cluster as well as address and credential. Without the
			// cluster every tunnelled upstream sharing a credential would
			// collapse onto one handler, and that handler forwards to exactly one
			// cluster — so a request for one model would be answered by another
			// cluster's.
			key := upstream.ClusterID + "\x00" + upstream.URL + "\x00" + upstream.CredentialID
			handler, ok := pools[key]
			if !ok {
				handler = r.factory(Target{
					ClusterID:    upstream.ClusterID,
					ClusterName:  upstream.ClusterName,
					URL:          address,
					CredentialID: upstream.CredentialID,
				})
				pools[key] = handler
			}
			used[key] = true

			weight := upstream.Weight
			if weight < 1 {
				weight = 1
			}
			route.Upstreams = append(route.Upstreams, Upstream{
				DeploymentID:    upstream.DeploymentID,
				ClusterID:       upstream.ClusterID,
				ClusterName:     upstream.ClusterName,
				URL:             upstream.URL,
				ServedModelName: upstream.ServedModelName,
				OutputModality:  upstream.OutputModality,
				Weight:          weight,
				Role:            upstream.Role,
				Ready:           upstream.AdmissionPhase == "running",
				Tunnelled:       address == nil,
				handler:         handler,
				reachable:       r.reachability(upstream.ClusterID, address != nil),
			})
			route.totalWeight += weight
		}

		// Carried across refreshes so a resync does not restart every route's
		// rotation at the same upstream. Keyed on the canonical, so churn in an
		// endpoint's alias set does not reset its weighted round-robin.
		if prior, ok := previous[routeKey(route.Name)]; ok && prior.cursor != nil {
			route.cursor = prior.cursor
		} else {
			route.cursor = newPicker()
		}
		built = append(built, route)
	}

	canonicals := index(next, built)

	for key := range pools {
		if !used[key] {
			delete(pools, key)
		}
	}

	r.mu.Lock()
	r.revision = table.Revision
	r.routes = next
	r.canonicals = canonicals
	r.proxies = pools
	r.mu.Unlock()

	r.observe(func(o Observer) { o.TableApplied(len(built), samples(built)) })

	slog.Info("gateway: routing table loaded",
		"revision", table.Revision, "endpoints", len(built), "upstreams", len(pools))
	return nil
}

// routeKey is how a public model id is indexed and looked up. Lowercased, so that
// `Kimi-K3` and `kimi-k3` are one route rather than two somebody has to keep in
// step — the plane lowercases what it stores and a caller types what they like.
func routeKey(name string) string { return strings.ToLower(strings.TrimSpace(name)) }

// index binds every name that reaches a route into one map, and returns the
// canonical names in sorted order.
//
// Canonicals are bound before any alias, so an alias can never shadow some
// other endpoint's own model id whatever order the plane listed them in. Within
// each pass the first binding of a key wins and a second is refused with an
// error in the log: the plane refuses this namespace collision at write time,
// so one arriving here is a misconfiguration, and picking between two bindings
// by table order would make routing depend on the order rows came back — worse
// than a loud complaint, because it is silent and it moves.
func index(into map[string]Route, routes []Route) []string {
	canonicals := make([]string, 0, len(routes))
	for _, route := range routes {
		name := routeKey(route.Name)
		if held, taken := into[name]; taken {
			slog.Error("gateway: two endpoints claim one model id, keeping the first",
				"model", name, "keeping", held.ID, "ignoring", route.ID)
			continue
		}
		into[name] = route
		canonicals = append(canonicals, route.Name)
	}
	for _, route := range routes {
		for _, alias := range route.Aliases {
			name := routeKey(alias)
			if name == "" {
				continue
			}
			if held, taken := into[name]; taken {
				slog.Error("gateway: an alias claims a model id that is already bound, keeping the first",
					"model", name, "keeping", held.ID, "ignoring", route.ID)
				continue
			}
			into[name] = route
		}
	}
	sort.Strings(canonicals)
	return canonicals
}

// reachability builds the liveness test for one upstream.
//
// An upstream with an address is always a candidate: whether the address answers
// is the proxy's problem and produces a 502, which is the truth. A tunnelled one
// is a candidate only while this replica holds a tunnel to its cluster — and with
// no pool at all it never is, so a gateway built without tunnels behaves exactly
// as it did when an addressless upstream was skipped outright.
func (r *Router) reachability(clusterID string, addressed bool) func() bool {
	if addressed {
		return nil
	}
	tunnels := r.tunnels
	if tunnels == nil {
		return func() bool { return false }
	}
	return func() bool { return tunnels.Live(clusterID) > 0 }
}

// observe calls the observer when there is one.
func (r *Router) observe(fn func(Observer)) {
	if r.observer != nil {
		fn(r.observer)
	}
}

// samples flattens a loaded table into what the observer reports on.
func samples(routes []Route) []metrics.UpstreamSample {
	flat := make([]metrics.UpstreamSample, 0, len(routes))
	for _, route := range routes {
		endpoint := route.ID
		if endpoint == "" {
			endpoint = metrics.EndpointUnknown
		}
		for _, upstream := range route.Upstreams {
			flat = append(flat, metrics.UpstreamSample{
				Endpoint:     endpoint,
				EndpointName: route.Name,
				Deployment:   upstream.DeploymentID,
				Cluster:      upstream.ClusterName,
				Ready:        upstream.Ready,
			})
		}
	}
	return flat
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
	route, ok := r.routes[routeKey(name)]
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
	names := make([]string, len(r.canonicals))
	copy(names, r.canonicals)
	return names
}

// NamesForTenant lists what this gateway serves on behalf of one tenant.
//
// A tenant's own catalog, for the public listener (docs/decisions/0016): a caller
// identified as one tenant sees that tenant's models and no others', the same
// way a resolve outside it reads as "no such model" rather than "here is what
// you cannot have."
func (r *Router) NamesForTenant(tenantID string) []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.canonicals))
	for _, name := range r.canonicals {
		if r.routes[routeKey(name)].TenantID == tenantID {
			names = append(names, name)
		}
	}
	return names
}

// ClusterReach is one cluster in the loaded table and how this replica can get
// to it.
type ClusterReach struct {
	ClusterID   string
	ClusterName string
	// Tunnelled means at least one of its upstreams has no published address and
	// can only be served over a tunnel.
	Tunnelled bool
	// Reachable means at least one of its upstreams can take a request now: a
	// live tunnel, or an address to dial.
	Reachable bool
}

// ClusterReach lists every cluster the loaded table routes to, with whether this
// replica can currently reach it.
//
// It is what readiness is computed from. A replica holding no tunnel to any
// cluster in its own table can serve nothing and must say so, and this is the
// only place both halves of that sentence — the table and the tunnels — are
// known together.
func (r *Router) ClusterReach() []ClusterReach {
	r.mu.RLock()
	routes := r.routes
	r.mu.RUnlock()

	byCluster := make(map[string]ClusterReach)
	for name, route := range routes {
		if routeKey(route.Name) != name {
			continue
		}
		for _, upstream := range route.Upstreams {
			reach := byCluster[upstream.ClusterID]
			reach.ClusterID = upstream.ClusterID
			reach.ClusterName = upstream.ClusterName
			reach.Tunnelled = reach.Tunnelled || upstream.Tunnelled
			reach.Reachable = reach.Reachable || upstream.Reachable()
			byCluster[upstream.ClusterID] = reach
		}
	}

	clusters := make([]ClusterReach, 0, len(byCluster))
	for _, reach := range byCluster {
		clusters = append(clusters, reach)
	}
	sort.Slice(clusters, func(i, j int) bool { return clusters[i].ClusterID < clusters[j].ClusterID })
	return clusters
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
