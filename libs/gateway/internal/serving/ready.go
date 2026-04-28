package serving

import (
	"context"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// ReadinessChecker answers whether this replica should be sent traffic, and why
// not when it should not.
//
// Nil in a Config means always ready, which is the correct answer for a gateway
// with no tunnels: everything it routes to has an address.
type ReadinessChecker interface {
	Ready() (bool, string)
}

// Reach is the routing table's account of which clusters it routes to.
// routing.Router implements it.
type Reach interface {
	ClusterReach() []routing.ClusterReach
}

// CoverageObserver records which clusters this replica can reach. Metrics only.
type CoverageObserver interface {
	TunnelCoverage(samples []metrics.TunnelSample)
}

// Readiness gates traffic on this replica actually being able to carry it.
//
// A tunnel is dialled to one replica, not to a Service, so coverage is per-pod:
// a replica holding no tunnel to any cluster in its own routing table can serve
// nothing, and a load balancer has no way to know that unless it says so. That is
// what this answers, and it is the reason a gateway replica has a stable identity —
// coverage has to be a fact rather than a hope about where the last dial landed.
//
// The gate does not apply to the tunnel listener. That listener is a separate
// port and never consults this type, because a replica that could only be dialled
// once ready could never become ready: the gate would be the thing holding it
// shut. The Service in front of the tunnel port must publish not-ready addresses
// for the same reason.
type Readiness struct {
	table    Reach
	coverage CoverageObserver
	draining atomic.Bool
}

// NewReadiness builds the gate. coverage may be nil.
func NewReadiness(table Reach, coverage CoverageObserver) *Readiness {
	return &Readiness{table: table, coverage: coverage}
}

// Drain makes this replica report not-ready without touching anything it is
// already serving.
//
// Called the moment a shutdown signal arrives, before the drain itself: an
// endpoint removal takes a few hundred milliseconds to propagate, and the
// requests that arrive during those milliseconds are the ones a rollout severs.
func (r *Readiness) Drain() { r.draining.Store(true) }

// Ready reports whether this replica can carry traffic.
//
// An empty table is ready. A gateway that has been given no endpoints yet can
// answer GET /v1/models and a correct 404 for everything else, and holding it out
// of its Service would mean a fresh install never joined one — the failure would
// look like the data plane being broken rather than like there being nothing to
// serve.
//
// A table with clusters in it needs at least one this replica can reach. Not all
// of them: a single cluster whose agent is being rolled must not take every
// other model out of this replica, and a replica that can serve some of the table
// is worth more in the Service than out of it.
func (r *Readiness) Ready() (bool, string) {
	if r.draining.Load() {
		return false, "draining"
	}

	clusters := r.Coverage()
	if len(clusters) == 0 {
		return true, "no clusters in the routing table"
	}
	reachable := 0
	for _, cluster := range clusters {
		if cluster.Reachable {
			reachable++
		}
	}
	if reachable == 0 {
		return false, fmt.Sprintf("no tunnel and no address for any of the %d clusters in the routing table", len(clusters))
	}
	return true, fmt.Sprintf("%d of %d clusters reachable", reachable, len(clusters))
}

// Coverage reports this replica's reachability to the metrics and returns what it
// reported.
func (r *Readiness) Coverage() []routing.ClusterReach {
	clusters := r.table.ClusterReach()
	if r.coverage == nil {
		return clusters
	}
	samples := make([]metrics.TunnelSample, 0, len(clusters))
	for _, cluster := range clusters {
		samples = append(samples, metrics.TunnelSample{
			ClusterID: cluster.ClusterID,
			Cluster:   cluster.ClusterName,
			Reachable: cluster.Reachable,
		})
	}
	r.coverage.TunnelCoverage(samples)
	return clusters
}

// RunCoverage keeps the coverage gauge fresh until ctx is done.
//
// The readiness probe refreshes the same gauge, and that is not enough on its
// own: the probe's path is a manifest setting, so a cluster whose readinessProbe
// still points at /healthz would export no coverage at all — and coverage is the
// number that says why requests for a cluster are getting none of this replica.
// An alert must not depend on how somebody configured a kubelet.
func (r *Readiness) RunCoverage(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.Coverage()
		}
	}
}
