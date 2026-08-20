package serving

import (
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// Tunnels is the tunnel pool as this package needs it.
//
// Two methods, and the split between them is the design: Live is asked on the
// request path and must be cheap, RoundTripper is asked once when the table is
// loaded and hands back an ordinary http.RoundTripper. Nothing here writes
// anything back — reachability is measured on this process and never becomes
// observed state.
type Tunnels interface {
	Live(clusterID string) int
	RoundTripper(clusterID string) http.RoundTripper
}

// ClusterConfig describes one cluster's upstream as the routing table has it.
type ClusterConfig struct {
	// ClusterID is which cluster this handler forwards to. It is the tunnel
	// pool's key and the only identity the data path has.
	ClusterID string
	// ClusterName is for logs.
	ClusterName string
	// Headers are the credential this address sits behind, applied outbound.
	Headers http.Header
	// URL is the cluster's published serving address, nil when it has none.
	URL *url.URL
	// Tunnels, when set, is where a tunnelled request goes.
	Tunnels Tunnels
	// ResponseHeaderTimeout bounds how long the cluster may hold a request
	// without answering, on whichever of the two paths it is forwarded down.
	// Zero takes tunnel.DefaultResponseHeaderTimeout.
	//
	// One field and not two on purpose. The tunnelled path and the direct path
	// are alternatives rather than layers — a request takes one or the other —
	// so a gateway that bounded them differently would give the same caller two
	// different walls depending on whether the cluster's agent happened to be
	// connected.
	ResponseHeaderTimeout time.Duration
}

// NewClusterHandler forwards to one cluster, over its tunnel when there is one
// and to its published address when there is not.
//
// Both modes coexist on purpose. The tunnel is how a cluster in a network we do
// not control is reached, and it is preferred whenever one is live; ClusterServing
// is still there and a cluster that publishes an address is still dialled at it,
// which is the fallback for a cluster whose agent has not rolled out yet.
//
// A cluster with neither answers 503, not 502. Nothing was attempted, so
// reporting an upstream failure would blame a cluster this replica never spoke
// to — and it is the routing table's Pick that normally catches this, so reaching
// the branch below means a tunnel died between the pick and the forward.
func NewClusterHandler(cfg ClusterConfig) http.Handler {
	handler := &clusterHandler{clusterID: cfg.ClusterID, tunnels: cfg.Tunnels}
	if cfg.Tunnels != nil {
		handler.tunnelled = NewTunnelProxy(cfg.ClusterID, cfg.Headers, cfg.Tunnels, cfg.ResponseHeaderTimeout)
	}
	if cfg.URL != nil {
		handler.direct = NewReverseProxy(cfg.URL, cfg.Headers, cfg.ResponseHeaderTimeout)
	}
	if handler.tunnelled == nil && handler.direct == nil {
		slog.Warn("gateway: cluster has no tunnel and no serving address, it will report no capacity",
			"cluster", cfg.ClusterName, "cluster_id", cfg.ClusterID)
	}
	return handler
}

type clusterHandler struct {
	clusterID string
	tunnels   Tunnels
	tunnelled http.Handler
	direct    http.Handler
}

func (h *clusterHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if h.tunnelled != nil && h.tunnels.Live(h.clusterID) > 0 {
		h.tunnelled.ServeHTTP(w, r)
		return
	}
	if h.direct != nil {
		h.direct.ServeHTTP(w, r)
		return
	}
	MarkOutcome(r, metrics.OutcomeNoUpstream)
	writeError(w, http.StatusServiceUnavailable, "no_capacity",
		"The cluster serving this model is not currently reachable.")
}

// NewTunnelProxy forwards to a cluster over the connection it dialled out.
//
// It is the ordinary reverse proxy with one field replaced. The URL is a
// placeholder that never resolves — the connection is the address — and the
// transport is the pool's, so FlushInterval, ModifyResponse and the usage
// accounting all behave exactly as they do against a public address. That the
// seam is one parameter wide is the whole reason the tunnel was built as an
// http.RoundTripper.
func NewTunnelProxy(clusterID string, headers http.Header, tunnels Tunnels, headerTimeout time.Duration) *httputil.ReverseProxy {
	proxy := NewReverseProxy(tunnel.Authority(clusterID), headers, headerTimeout)
	proxy.Transport = tunnels.RoundTripper(clusterID)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		markTunnelError(w, r, clusterID, err)
	}
	return proxy
}

// markTunnelError answers a tunnelled request that never got a response.
//
// The classification is the point. Without it an agent rollout, a saturated
// cluster and a cluster that genuinely failed are all one 502 with one outcome,
// and two of those three are this project's own infrastructure rather than a
// model being down. The mapping is the one tunnel.Fault documents.
func markTunnelError(w http.ResponseWriter, r *http.Request, clusterID string, err error) {
	fault := tunnel.Classify(err)
	slog.Error("gateway: tunnelled request failed",
		"request_id", r.Header.Get(headerRequestID), "cluster_id", clusterID, "path", r.URL.Path, "fault", fault.String(), "err", err)

	switch fault {
	case tunnel.FaultNoTunnel:
		// Nothing was attempted: the agent went away between the pick and
		// the forward. Capacity, not a cluster failure.
		MarkOutcome(r, metrics.OutcomeNoUpstream)
		writeError(w, http.StatusServiceUnavailable, "no_capacity",
			"The cluster serving this model is not currently reachable.")
	case tunnel.FaultSaturated:
		MarkOutcome(r, metrics.OutcomeShed)
		writeError(w, http.StatusServiceUnavailable, "no_capacity",
			"The cluster serving this model is at capacity.")
	case tunnel.FaultHeaderTimeout:
		MarkOutcome(r, metrics.OutcomeUpstreamTimeout)
		writeError(w, http.StatusBadGateway, "upstream_unavailable",
			"The cluster serving this model did not respond.")
	case tunnel.FaultCanceled:
		MarkOutcome(r, metrics.OutcomeCanceled)
		writeError(w, http.StatusBadGateway, "upstream_unavailable",
			"The cluster serving this model did not respond.")
	default:
		// Churn folds into upstream_error deliberately: an agent rollout
		// counts slightly against serving availability rather than into a
		// carve-out nothing can falsify.
		MarkOutcome(r, metrics.OutcomeUpstreamError)
		writeError(w, http.StatusBadGateway, "upstream_unavailable",
			"The cluster serving this model did not respond.")
	}
}
