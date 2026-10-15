package agent

import (
	"net/http"

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
// it has been accepted — the agent dials one address and the gateway's answer is what names who took
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
}

// Reasons a dial failed, as the `reason` label on hangar_agent_dial_failures_total.
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
		Name: "hangar_agent_gateways_expected",
		Help: "Gateway replicas the gateway last reported it was running.",
	})
	m.tunnels = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_agent_tunnels",
		Help: "Established tunnels to each gateway replica.",
	}, []string{labelGateway})
	m.up = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_agent_gateway_up",
		Help: "1 when at least one tunnel to the gateway replica is established.",
	}, []string{labelGateway})
	m.attempts = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "hangar_agent_dial_attempts_total",
		Help: "Outbound tunnel dials attempted.",
	})
	m.failures = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_agent_dial_failures_total",
		Help: "Outbound tunnel dials that did not produce a tunnel, per reason.",
	}, []string{"reason"})
	// Not a failure and deliberately not folded into one. A dial that landed on a replica this
	// agent already covers is the load balancer choosing without knowing what is held, which is
	// how coverage is reached at all; a rate that never settles is the signal, and that is only
	// readable if it is its own series.
	m.duplicates = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_agent_dial_duplicates_total",
		Help: "Dials answered by a replica that already had its quota of tunnels.",
	}, []string{labelGateway})
	m.established = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_agent_tunnels_established_total",
		Help: "Tunnels that completed the preface, per gateway replica.",
	}, []string{labelGateway})
	m.closed = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_agent_tunnels_closed_total",
		Help: "Tunnels that ended, per gateway replica.",
	}, []string{labelGateway})
	m.sessions = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name: "hangar_agent_tunnel_seconds",
		Help: "How long each tunnel lasted, per gateway replica.",
		// Wide and coarse: the question this answers is whether tunnels live for hours or are
		// being churned every few seconds, and nothing in between matters.
		Buckets: []float64{1, 10, 60, 300, 1800, 7200, 43200},
	}, []string{labelGateway})

	m.registry.MustRegister(m.expected, m.tunnels, m.up, m.attempts, m.failures, m.duplicates,
		m.established, m.closed, m.sessions)
	m.attempts.Add(0)
	for _, reason := range []string{reasonDial, reasonUnauthorized, reasonHandshake, reasonRefused} {
		m.failures.WithLabelValues(reason).Add(0)
	}
	return m
}

// Registry exposes the registry so a caller can register its own collectors on it.
func (m *Metrics) Registry() *prometheus.Registry { return m.registry }

// Handler serves the exposition.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{})
}

// expect records how many replicas the gateway says it is running.
func (m *Metrics) expect(replicas int) {
	m.expected.Set(float64(replicas))
}

// track pre-creates every series for a replica the moment it is first reached, so that a replica
// whose tunnel then drops reads as zero rather than as absent.
//
// It cannot be done up front any more, because the agent is no longer told which replicas exist —
// hangar_agent_gateways_expected is what says how many series there should be.
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
