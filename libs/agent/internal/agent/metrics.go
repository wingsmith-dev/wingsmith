package agent

import (
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics is the agent's exposition.
//
// Per replica wherever a replica is known, always. One aggregate number cannot say that the agent is
// holding four tunnels to three of four gateway replicas, and that is the only shape of failure this
// process has: a gateway replica with no tunnel to this cluster serves none of its models and looks,
// from every other vantage point, entirely healthy.
//
// The dial counters are the exception and carry no replica label, because a dial has no replica until
// it has been accepted: the agent dials one address and the gateway's answer is what names who took
// it. A failed dial genuinely belongs to nobody.
type Metrics struct {
	registry *prometheus.Registry

	expected    prometheus.Gauge
	tunnels     *prometheus.GaugeVec
	up          *prometheus.GaugeVec
	attempts    prometheus.Counter
	failures    *prometheus.CounterVec
	duplicates  *prometheus.CounterVec
	established *prometheus.CounterVec
	closed      *prometheus.CounterVec
	sessions    *prometheus.HistogramVec

	requests *prometheus.CounterVec
	inFlight prometheus.Gauge
	duration prometheus.Histogram
}

// Reasons a dial failed, as the `reason` label on wingsmith_agent_dial_failures_total.
const (
	reasonDial         = "dial"
	reasonUnauthorized = "unauthorized"
	reasonHandshake    = "handshake"
	// reasonRefused is a gateway that answered and turned the dial away for a reason that is
	// not the credential: the plane was unreachable, or it is at its tunnel limit.
	reasonRefused       = "refused"
	labelGateway        = "gateway"
	metricsRegistryPath = "/metrics"
)

// NewMetrics builds the exposition on its own registry.
func NewMetrics() *Metrics {
	m := &Metrics{registry: prometheus.NewRegistry()}

	// The one number an alert should be written against, together with a count of the series
	// below: coverage is complete when this many replicas each have a tunnel, and the gateway is
	// the only thing that knows what this many is.
	m.expected = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "wingsmith_agent_gateways_expected",
		Help: "Gateway replicas the gateway last reported it was running.",
	})
	m.tunnels = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "wingsmith_agent_tunnels",
		Help: "Established tunnels to each gateway replica.",
	}, []string{labelGateway})
	m.up = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "wingsmith_agent_gateway_up",
		Help: "1 when at least one tunnel to the gateway replica is established.",
	}, []string{labelGateway})
	m.attempts = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "wingsmith_agent_dial_attempts_total",
		Help: "Outbound tunnel dials attempted.",
	})
	m.failures = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wingsmith_agent_dial_failures_total",
		Help: "Outbound tunnel dials that did not produce a tunnel, per reason.",
	}, []string{"reason"})
	// Not a failure, and intentionally not folded into one. A dial that landed on a replica this
	// agent already covers is the load balancer choosing without knowing what is held, which is
	// how coverage is reached at all; a rate that never settles is the signal, and that is only
	// readable if it is its own series.
	m.duplicates = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wingsmith_agent_dial_duplicates_total",
		Help: "Dials answered by a replica that already had its quota of tunnels.",
	}, []string{labelGateway})
	m.established = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wingsmith_agent_tunnels_established_total",
		Help: "Tunnels that completed the preface, per gateway replica.",
	}, []string{labelGateway})
	m.closed = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wingsmith_agent_tunnels_closed_total",
		Help: "Tunnels that ended, per gateway replica.",
	}, []string{labelGateway})
	m.sessions = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name: "wingsmith_agent_tunnel_seconds",
		Help: "How long each tunnel lasted, per gateway replica.",
		// Wide and coarse: the question this answers is whether tunnels live for hours or are
		// being churned every few seconds, and nothing in between matters.
		Buckets: []float64{1, 10, 60, 300, 1800, 7200, 43200},
	}, []string{labelGateway})

	// The requests that came down a tunnel, by what became of them. No model, deployment or
	// caller label: the gateway already counts per endpoint, and this hop exists to say whether a
	// failure happened inside the cluster or before it.
	m.requests = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wingsmith_agent_requests_total",
		Help: "Requests that arrived over a tunnel, by outcome and the status class answered. forwarded with code 5xx is the frontend's own error; frontend_unavailable is a frontend that did not answer; aborted is a response cut after it started.",
	}, []string{"outcome", "code"})
	m.inFlight = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "wingsmith_agent_requests_in_flight",
		Help: "Requests this agent is serving now, across every tunnel.",
	})
	m.duration = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name: "wingsmith_agent_request_duration_seconds",
		Help: "Time from a request's arrival over the tunnel to its last response byte.",
		// The gateway's request buckets, so the two hops can be read side by side.
		Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300},
	})

	m.registry.MustRegister(m.expected, m.tunnels, m.up, m.attempts, m.failures, m.duplicates,
		m.established, m.closed, m.sessions, m.requests, m.inFlight, m.duration)
	m.attempts.Add(0)
	for _, reason := range []string{reasonDial, reasonUnauthorized, reasonHandshake, reasonRefused} {
		m.failures.WithLabelValues(reason).Add(0)
	}
	return m
}

// WatchDiscovery publishes how long frontend discovery has gone without an answer from the API
// server, read from age at every scrape.
func (m *Metrics) WatchDiscovery(age func() time.Duration) {
	m.registry.MustRegister(prometheus.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "wingsmith_agent_frontend_discovery_age_seconds",
		Help: "Seconds since frontend discovery last got an answer from the API server. Requests keep going to the addresses last seen while it grows, but those addresses no longer follow a redeployed frontend.",
	}, func() float64 { return age().Seconds() }))
}

// Registry exposes the registry so a caller can register its own collectors on it.
func (m *Metrics) Registry() *prometheus.Registry { return m.registry }

// Handler serves the exposition.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

// RequestStarted counts a request in flight. It implements dynamo.Observer.
func (m *Metrics) RequestStarted() {
	m.inFlight.Inc()
}

// RequestFinished records a request that has ended. It implements dynamo.Observer.
func (m *Metrics) RequestFinished(outcome, code string, elapsed time.Duration) {
	m.inFlight.Dec()
	m.requests.WithLabelValues(outcome, code).Inc()
	m.duration.Observe(elapsed.Seconds())
}

// expect records how many replicas the gateway says it is running.
func (m *Metrics) expect(replicas int) {
	m.expected.Set(float64(replicas))
}

// track pre-creates every series for a replica the moment it is first reached, so that a replica
// whose tunnel then drops reads as zero, not as absent.
//
// It cannot be done up front any more, because the agent is no longer told which replicas exist:
// wingsmith_agent_gateways_expected is what says how many series there should be.
func (m *Metrics) track(gateway string) {
	m.tunnels.WithLabelValues(gateway).Set(0)
	m.up.WithLabelValues(gateway).Set(0)
	m.duplicates.WithLabelValues(gateway).Add(0)
	m.established.WithLabelValues(gateway).Add(0)
	m.closed.WithLabelValues(gateway).Add(0)
}

// forget drops every series for a replica the gateway has scaled past, so a zero gauge does not
// alert forever about something that no longer exists.
func (m *Metrics) forget(gateway string) {
	m.tunnels.DeleteLabelValues(gateway)
	m.up.DeleteLabelValues(gateway)
	m.duplicates.DeleteLabelValues(gateway)
	m.established.DeleteLabelValues(gateway)
	m.closed.DeleteLabelValues(gateway)
	m.sessions.DeleteLabelValues(gateway)
}

func (m *Metrics) dialled() {
	m.attempts.Inc()
}

func (m *Metrics) dialFailed(reason string) {
	m.failures.WithLabelValues(reason).Inc()
}

func (m *Metrics) duplicate(gateway string) {
	m.duplicates.WithLabelValues(gateway).Inc()
}

func (m *Metrics) tunnelOpened(gateway string, live int) {
	m.established.WithLabelValues(gateway).Inc()
	m.observe(gateway, live)
}

func (m *Metrics) tunnelClosed(gateway string, live int, seconds float64) {
	m.closed.WithLabelValues(gateway).Inc()
	m.sessions.WithLabelValues(gateway).Observe(seconds)
	m.observe(gateway, live)
}

func (m *Metrics) observe(gateway string, live int) {
	m.tunnels.WithLabelValues(gateway).Set(float64(live))
	if live > 0 {
		m.up.WithLabelValues(gateway).Set(1)
		return
	}
	m.up.WithLabelValues(gateway).Set(0)
}
