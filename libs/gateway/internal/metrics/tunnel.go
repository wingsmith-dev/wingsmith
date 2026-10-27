package metrics

import "github.com/prometheus/client_golang/prometheus"

// The tunnel series are labelled cluster_id, not cluster, because the
// tunnel pool is keyed by the plane's cluster uid and a cluster renames freely:
// a name label here would split one cluster's coverage in two the first time
// somebody renamed it, and the break would look like an agent outage. The
// human-readable name rides alongside as a second label on the coverage gauge,
// where it is for panels and never joined on.
var (
	tunnelLabels   = []string{"cluster_id"}
	coverageLabels = []string{"cluster_id", "cluster"}
)

// TunnelSample is one cluster in the routing table and how this replica reaches
// it.
type TunnelSample struct {
	ClusterID string
	Cluster   string
	// Reachable means at least one of its upstreams can take a request now.
	Reachable bool
}

// tunnelEvents are the tunnel series registered by registerTunnels itself.
type tunnelEvents struct {
	duplicates prometheus.Counter
	revoked    *prometheus.CounterVec
	retries    *prometheus.CounterVec
}

// registerTunnels builds the tunnel series. Called from New.
func (r *Recorder) registerTunnels() {
	r.tunnelEvents = tunnelEvents{
		duplicates: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "hangar_gateway_tunnel_dials_duplicate_total",
			Help: "Agent dials that landed on this replica after the agent already covered it. Not a refusal: it is how an agent behind one address finds every replica. A rate that never settles means the replica count the agents were told is wrong.",
		}),
		revoked: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "hangar_gateway_tunnels_revoked_total",
			Help: "Open tunnels closed because the plane no longer accepts their token, or now resolves it to another cluster.",
		}, tunnelLabels),
		retries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "hangar_gateway_tunnel_retries_total",
			Help: "Requests whose tunnel died under them with no GOAWAY, by what became of them: resent down another tunnel, not resent because the agent may already have forwarded them (written), or out of attempts (exhausted).",
		}, []string{"cluster_id", "outcome"}),
	}
	r.registry.MustRegister(r.tunnelEvents.duplicates, r.tunnelEvents.revoked, r.tunnelEvents.retries)

	r.tunnels = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_gateway_tunnels",
		Help: "Live tunnels this replica holds to a cluster. Per-replica by nature: an agent dials one pod, not a Service, so this is coverage of this process and not of the deployment.",
	}, tunnelLabels)

	r.tunnelDials = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hangar_gateway_tunnel_dials_rejected_total",
		Help: "Agent dials that never became tunnels, by reason. unauthorized is a credential problem and unavailable is the plane being unreachable; the two need different people. limit is a dial that authenticated while its cluster or this replica held as many tunnels as allowed, and throttled one from an address that kept failing or had too many handshakes open.",
	}, []string{"reason"})

	r.tunnelCoverage = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hangar_gateway_tunnel_coverage",
		Help: "0 when this replica cannot reach a cluster its own routing table routes to, 1 when it can. The alert is on the zero: it means requests for that cluster's models get no capacity from this pod.",
	}, coverageLabels)
}

// TunnelOpened records a tunnel this replica adopted.
func (r *Recorder) TunnelOpened(clusterID string, live int) {
	r.tunnels.WithLabelValues(clusterID).Set(float64(live))
}

// TunnelClosed records a tunnel this replica lost.
//
// The gauge is set, not decremented, and the count comes from the pool: a
// decrement that raced a re-dial would drift, and a coverage number that drifts
// is worse than one that is occasionally stale.
func (r *Recorder) TunnelClosed(clusterID string, live int) {
	r.tunnels.WithLabelValues(clusterID).Set(float64(live))
}

// StreamSample is one cluster's stream use across this replica's live tunnels.
type StreamSample struct {
	ClusterID string
	Active    int
	Capacity  int
}

var (
	streamsActiveDesc = prometheus.NewDesc("hangar_gateway_tunnel_streams_active",
		"Requests open on this replica's live tunnels to a cluster. Read against hangar_gateway_tunnel_streams_capacity: when the two meet, new requests for the cluster are refused as no capacity until a stream finishes.",
		tunnelLabels, nil)
	streamsCapacityDesc = prometheus.NewDesc("hangar_gateway_tunnel_streams_capacity",
		"Concurrent streams the agents allow on this replica's live tunnels to a cluster, summed over the tunnels.",
		tunnelLabels, nil)
)

// streamCollector reads stream use from the pool at scrape time. A cluster with
// no live tunnel has no series; hangar_gateway_tunnel_coverage covers that case.
type streamCollector struct {
	source func() []StreamSample
}

func (c streamCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- streamsActiveDesc
	ch <- streamsCapacityDesc
}

func (c streamCollector) Collect(ch chan<- prometheus.Metric) {
	for _, sample := range c.source() {
		ch <- prometheus.MustNewConstMetric(streamsActiveDesc, prometheus.GaugeValue, float64(sample.Active), sample.ClusterID)
		ch <- prometheus.MustNewConstMetric(streamsCapacityDesc, prometheus.GaugeValue, float64(sample.Capacity), sample.ClusterID)
	}
}

// WatchTunnelStreams publishes the stream use source reports, read at every
// scrape. Call it once, with the tunnel pool.
func (r *Recorder) WatchTunnelStreams(source func() []StreamSample) {
	r.registry.MustRegister(streamCollector{source: source})
}

// TunnelDialRejected records a dial that did not become a tunnel. Most did not
// authenticate; a "limit" dial did, and was refused for the tunnel cap.
func (r *Recorder) TunnelDialRejected(reason string) {
	r.tunnelDials.WithLabelValues(reason).Inc()
}

// TunnelDialDuplicate records a dial to a replica the agent already covers.
// It implements tunnel.DuplicateObserver.
func (r *Recorder) TunnelDialDuplicate() {
	r.tunnelEvents.duplicates.Inc()
}

// TunnelRevoked records an open tunnel closed for its token. It implements
// tunnel.RevocationObserver.
func (r *Recorder) TunnelRevoked(clusterID string) {
	r.tunnelEvents.revoked.WithLabelValues(clusterID).Inc()
}

// TunnelRetry records what became of a request whose tunnel died. It
// implements tunnel.RetryObserver.
func (r *Recorder) TunnelRetry(clusterID, outcome string) {
	r.tunnelEvents.retries.WithLabelValues(clusterID, outcome).Inc()
}

// TunnelCoverage records which clusters in the loaded table this replica can
// reach.
//
// It is reset first, so a cluster that has left the routing table stops
// reporting instead of freezing at its last value: a stuck 0 on a cluster
// nobody routes to any more is an alert that cannot be cleared.
func (r *Recorder) TunnelCoverage(samples []TunnelSample) {
	r.tunnelCoverage.Reset()
	for _, sample := range samples {
		covered := 0.0
		if sample.Reachable {
			covered = 1
		}
		r.tunnelCoverage.WithLabelValues(sample.ClusterID, sample.Cluster).Set(covered)
	}
}
