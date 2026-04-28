// Package metrics is what the gateway exposes about itself on /metrics.
//
// It exists because availability cannot be measured from inside a cluster. A
// request that fails before it reaches one emits nothing a serving runtime can
// count: a model that is not served, an endpoint with nowhere to route, a
// credential that has gone stale, a cluster that cannot be dialled at all. Those
// are the requests that dominate an availability incident, and this hop is the
// only place in the system that sees every one of them.
//
// It is observability and not a ledger. Nothing here is metered or billed —
// two systems counting tokens is two systems disagreeing about an invoice — and
// nothing here carries an identity. Caller identity, API keys, client IPs and
// request ids are forbidden on every series in this package: they are unbounded,
// and putting one in a time series is the same mistake as putting one in a log
// nobody rotates.
//
// The cardinality bill is paid in labels that come from a plane object or a
// closed enum, and nowhere else.
package metrics

import (
	"net/http"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Reserved endpoint labels for requests that name no endpoint of ours.
//
// Both are underscore-prefixed and every plane uid carries a letter prefix, so
// neither can collide with a real one.
const (
	// EndpointCatalog is GET /v1/models, which is answered from the table
	// rather than by any endpoint.
	EndpointCatalog = "_catalog"
	// EndpointUnknown is a request that named a model we do not serve, or that
	// never got as far as naming one. It is not given an invented label: a
	// free-text model string on a series is an unbounded axis handed to
	// whoever is calling.
	EndpointUnknown = "_unknown"
)

// durationBuckets covers a completion, which is legitimately slow. A request
// that took four minutes is not hung, and a bucket layout that tops out at ten
// seconds cannot tell four minutes from forty.
var durationBuckets = []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300}

// ttftBuckets are tighter at the bottom: time to first token is the number a
// buyer feels, and the interesting part of its distribution is under a second.
var ttftBuckets = []float64{0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60}

var (
	// commonLabels ride on every request series. hangar_endpoint is the join
	// key, endpoint_name is for panels and is never joined on, origin keeps the
	// conformance suite out of the availability ratio.
	commonLabels = []string{"hangar_endpoint", "endpoint_name", "origin"}
	// routedLabels add where the request went. Empty strings, never absent
	// labels: the requests no cluster could have seen are the important class,
	// and an absent label makes that class queryable only by a negation over an
	// unbounded set.
	routedLabels = []string{"hangar_endpoint", "endpoint_name", "origin", "deployment", "cluster"}
)

// Recorder holds the series and the registry they live in.
type Recorder struct {
	registry *prometheus.Registry
	now      func() time.Time

	requests    *prometheus.CounterVec
	duration    *prometheus.HistogramVec
	ttft        *prometheus.HistogramVec
	ttftByInput *prometheus.HistogramVec
	tokens      *prometheus.CounterVec
	inFlight    *prometheus.GaugeVec
	upstreamUp  *prometheus.GaugeVec

	tunnels        *prometheus.GaugeVec
	tunnelDials    *prometheus.CounterVec
	tunnelCoverage *prometheus.GaugeVec

	tableConfirmed  *prometheus.Gauge
	tableRefreshes  *prometheus.CounterVec
	tableRefreshOK  prometheus.Gauge
	tableEndpoints  prometheus.Gauge
	tableConfirmUnx prometheus.Gauge

	// confirmedAt is when the plane last told us the loaded table is current,
	// as unix nanoseconds. Read at scrape time by the age gauge, so it is an
	// atomic rather than a mutex: a refresh must never contend with a scrape.
	confirmedAt atomic.Int64
}

// Option adjusts a Recorder at construction.
type Option func(*Recorder)

// WithClock replaces the clock. For tests; nothing in the binary sets it.
func WithClock(now func() time.Time) Option {
	return func(r *Recorder) { r.now = now }
}

// New builds a Recorder with its own registry.
//
// Its own, rather than the default one: the default registry is global state
// that any dependency can write to, and the series in here are a contract that
// a plane query and a frozen acceptance criteria version both read.
func New(opts ...Option) *Recorder {
	r := &Recorder{registry: prometheus.NewRegistry(), now: time.Now}
	for _, opt := range opts {
		opt(r)
	}

	r.requests = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_gateway_requests_total",
		Help: "Requests answered, by what happened to them. outcome is not derivable from status: a 429 issued to a caller over its declared limit and a 429 shed under load are the same code and only one is a failure.",
	}, append(append([]string{}, routedLabels...), "status", "outcome"))

	r.duration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "hangar_gateway_request_duration_seconds",
		Help:    "End-to-end time this hop spent on a request, first byte in to last byte out.",
		Buckets: durationBuckets,
	}, routedLabels)

	r.ttft = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "hangar_gateway_ttft_seconds",
		Help:    "Time from arrival to the first response byte forwarded to the caller.",
		Buckets: ttftBuckets,
	}, routedLabels)

	r.ttftByInput = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "hangar_gateway_ttft_by_input_seconds",
		Help:    "Time to first byte, banded by the request's own prompt length. Observed late, when usage arrives; a request that never disclosed one lands in input_bucket=\"unknown\".",
		Buckets: ttftBuckets,
	}, append(append([]string{}, commonLabels...), "input_bucket"))

	r.tokens = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_gateway_tokens_total",
		Help: "Tokens reported by the upstream's own usage object. Observability, not metering: this is not an invoice and must never be reconciled against one.",
	}, append(append([]string{}, routedLabels...), "direction"))

	r.inFlight = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_gateway_requests_in_flight",
		Help: "Requests currently being proxied to a cluster.",
	}, routedLabels)

	r.upstreamUp = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_gateway_upstream_up",
		Help: "1 when the plane's last table reported this deployment running. The plane's observation, not a probe of our own.",
	}, routedLabels)

	r.tableRefreshes = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_gateway_routing_table_refreshes_total",
		Help: "Attempts to re-read the routing table, by result. A failed refresh keeps the table already loaded, so error here is not an outage; error growing while age grows with it is.",
	}, []string{"result"})

	r.tableRefreshOK = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "hangar_gateway_routing_table_refresh_success",
		Help: "1 when the last attempt to re-read the routing table reached the plane, 0 when it did not.",
	})

	r.tableEndpoints = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "hangar_gateway_routing_table_endpoints",
		Help: "Endpoints in the table currently loaded.",
	})

	r.tableConfirmUnx = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "hangar_gateway_routing_table_confirmed_timestamp_seconds",
		Help: "Unix time at which the plane last confirmed the loaded table is current, either by sending a new one or by answering 304.",
	})

	// Age is computed at scrape rather than stored, so it is right in the
	// sample even if the process has been unable to refresh for an hour. It is
	// the plain reading of the timestamp gauge beside it, kept because it is
	// what a human curling this port during a plane outage wants to see.
	age := prometheus.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "hangar_gateway_routing_table_age_seconds",
		Help: "Seconds since the plane last confirmed the loaded table is current. Grows without bound while the plane is unreachable, and the gateway keeps serving the whole time.",
	}, func() float64 {
		confirmed := r.confirmedAt.Load()
		if confirmed == 0 {
			return 0
		}
		return r.now().Sub(time.Unix(0, confirmed)).Seconds()
	})

	r.registerTunnels()

	r.registry.MustRegister(
		r.requests, r.duration, r.ttft, r.ttftByInput, r.tokens,
		r.inFlight, r.upstreamUp,
		r.tunnels, r.tunnelDials, r.tunnelCoverage,
		r.tableRefreshes, r.tableRefreshOK, r.tableEndpoints, r.tableConfirmUnx, age,
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)

	// Pre-created so that a gateway that has never failed a refresh still
	// exposes the zero, and an alert on the counter has a series to subtract
	// from instead of a hole.
	for _, result := range []string{"applied", "unchanged", "error"} {
		r.tableRefreshes.WithLabelValues(result)
	}

	return r
}

// Handler serves the exposition format.
func (r *Recorder) Handler() http.Handler {
	return promhttp.HandlerFor(r.registry, promhttp.HandlerOpts{
		// A broken collector must not take the scrape down with it, and it must
		// certainly not take the request path down: this handler runs on its
		// own listener and answers with whatever it could gather.
		ErrorHandling: promhttp.ContinueOnError,
	})
}

// Gatherer exposes the registry for tests and for anything that wants to read
// the series without going through HTTP.
func (r *Recorder) Gatherer() prometheus.Gatherer { return r.registry }

// Sample is one finished request.
type Sample struct {
	Endpoint     string
	EndpointName string
	Origin       Origin
	Deployment   string
	Cluster      string
	Status       int
	Outcome      Outcome
	Duration     time.Duration

	// TTFT is the time to the first response byte, and is only meaningful for a
	// request that actually reached a cluster.
	TTFT     time.Duration
	HaveTTFT bool

	// Usage is what the upstream reported about itself. Absent when the
	// response carried none — never zero, because zero is a claim and absent
	// is the truth.
	InputTokens       int64
	OutputTokens      int64
	CachedInputTokens int64
	HaveUsage         bool
}

// Observe records one finished request.
func (r *Recorder) Observe(s Sample) {
	origin := s.Origin
	if origin == "" {
		origin = OriginLive
	}
	endpoint := s.Endpoint
	if endpoint == "" {
		endpoint = EndpointUnknown
	}

	r.requests.WithLabelValues(
		endpoint, s.EndpointName, string(origin), s.Deployment, s.Cluster,
		StatusLabel(s.Status), string(s.Outcome),
	).Inc()

	r.duration.WithLabelValues(
		endpoint, s.EndpointName, string(origin), s.Deployment, s.Cluster,
	).Observe(s.Duration.Seconds())

	if s.HaveTTFT {
		r.ttft.WithLabelValues(
			endpoint, s.EndpointName, string(origin), s.Deployment, s.Cluster,
		).Observe(s.TTFT.Seconds())

		r.ttftByInput.WithLabelValues(
			endpoint, s.EndpointName, string(origin), InputBucket(s.InputTokens, s.HaveUsage),
		).Observe(s.TTFT.Seconds())
	}

	if s.HaveUsage {
		labels := []string{endpoint, s.EndpointName, string(origin), s.Deployment, s.Cluster}
		r.tokens.WithLabelValues(append(labels, string(DirectionInput))...).Add(float64(s.InputTokens))
		r.tokens.WithLabelValues(append(labels, string(DirectionOutput))...).Add(float64(s.OutputTokens))
		r.tokens.WithLabelValues(append(labels, string(DirectionCachedInput))...).Add(float64(s.CachedInputTokens))
	}
}

// InFlight marks a request as being proxied and returns the release.
func (r *Recorder) InFlight(endpoint, endpointName string, origin Origin, deployment, cluster string) func() {
	gauge := r.inFlight.WithLabelValues(endpoint, endpointName, string(origin), deployment, cluster)
	gauge.Inc()
	return gauge.Dec
}

// UpstreamSample is one address behind one endpoint, as the last table had it.
type UpstreamSample struct {
	Endpoint     string
	EndpointName string
	Deployment   string
	Cluster      string
	Ready        bool
}

// TableApplied records a refresh that replaced the table.
//
// The upstream gauges are reset first, so an endpoint or a deployment that has
// left the table stops reporting rather than freezing at its last value — a
// stuck 1 on a deployment that no longer exists is worse than no series.
func (r *Recorder) TableApplied(endpoints int, upstreams []UpstreamSample) {
	r.upstreamUp.Reset()
	for _, upstream := range upstreams {
		ready := 0.0
		if upstream.Ready {
			ready = 1
		}
		r.upstreamUp.WithLabelValues(
			upstream.Endpoint, upstream.EndpointName, string(OriginLive),
			upstream.Deployment, upstream.Cluster,
		).Set(ready)
	}
	r.tableEndpoints.Set(float64(endpoints))
	r.tableRefreshes.WithLabelValues("applied").Inc()
	r.confirm()
}

// TableUnchanged records a refresh the plane answered with "still current".
//
// It confirms the table exactly as much as a new one does. Age here is time
// since the plane last vouched for what we hold, not time since it last
// changed — a table that has not changed in a week is healthy, and a table
// nobody has been able to confirm for ten minutes is the thing worth seeing.
func (r *Recorder) TableUnchanged() {
	r.tableRefreshes.WithLabelValues("unchanged").Inc()
	r.confirm()
}

// TableRefreshFailed records a refresh that did not reach the plane.
//
// It deliberately does not touch the confirmation time. The gateway keeps
// serving the table it has — a control-plane outage must not become a data
// plane outage — and the age gauge going up while this counter goes up is
// exactly the shape of that state.
func (r *Recorder) TableRefreshFailed() {
	r.tableRefreshes.WithLabelValues("error").Inc()
	r.tableRefreshOK.Set(0)
}

func (r *Recorder) confirm() {
	now := r.now()
	r.confirmedAt.Store(now.UnixNano())
	r.tableConfirmUnx.Set(float64(now.UnixNano()) / 1e9)
	r.tableRefreshOK.Set(1)
}
