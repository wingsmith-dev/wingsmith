package main

import (
	"log/slog"
	"time"
)

// defaultShutdownGrace is Kubernetes' default terminationGracePeriodSeconds.
// HANGAR_GATEWAY_SHUTDOWN_GRACE must match the pod's value: the drain phases
// are cut from it, and a kubelet that sends SIGKILL before they end cuts
// whatever generation is still running.
const defaultShutdownGrace = 30 * time.Second

// shutdownMargin is left unspent at the end of the grace period, for process
// exit and the kubelet's own timing.
const shutdownMargin = 3 * time.Second

// metricsShutdownTimeout bounds the last phase, closing the scrape listener.
const metricsShutdownTimeout = time.Second

// drainPlan is how long each shutdown phase may take, in the order they run.
type drainPlan struct {
	// delay keeps the listeners accepting after readiness drops, while
	// endpoint removal reaches every load balancer.
	delay time.Duration
	// requests is each request listener's budget to finish what it is serving.
	// The listeners drain concurrently, so it is counted once.
	requests time.Duration
	// tunnels is the tunnel listener's budget, after the request listeners.
	tunnels time.Duration
}

// total is the time from SIGTERM to the end of the last phase.
func (p drainPlan) total() time.Duration {
	return p.delay + p.requests + p.tunnels + metricsShutdownTimeout
}

// defaultDrainPlan splits a grace period across the phases: up to 5s each for
// the propagation delay and the tunnel drain, the margin and the metrics
// shutdown set aside, and everything left to the requests. At the default 30s
// grace that is 5s, 16s and 5s, ending 3s before the kubelet's SIGKILL.
func defaultDrainPlan(grace time.Duration) drainPlan {
	short := min(5*time.Second, grace/6)
	requests := max(grace-2*short-metricsShutdownTimeout-shutdownMargin, 0)
	return drainPlan{delay: short, requests: requests, tunnels: short}
}

// readDrainPlan reads the grace period and the phase overrides from the
// environment. A phase not set explicitly takes its share of the grace period;
// an explicit set that overruns it is kept and warned about, since the operator
// may have raised terminationGracePeriodSeconds without telling this process.
func readDrainPlan() (drainPlan, time.Duration) {
	grace := envDurationDefault("HANGAR_GATEWAY_SHUTDOWN_GRACE", defaultShutdownGrace)
	def := defaultDrainPlan(grace)
	plan := drainPlan{
		delay:    envDurationDefault("HANGAR_GATEWAY_DRAIN_DELAY", def.delay),
		requests: envDurationDefault("HANGAR_GATEWAY_DRAIN_TIMEOUT", def.requests),
		tunnels:  envDurationDefault("HANGAR_GATEWAY_TUNNEL_DRAIN_TIMEOUT", def.tunnels),
	}
	if plan.total() > grace-shutdownMargin {
		slog.Warn("gateway: the drain phases outlast the shutdown grace period; the pod will be killed mid-drain unless terminationGracePeriodSeconds and HANGAR_GATEWAY_SHUTDOWN_GRACE are raised",
			"grace", grace, "drain_total", plan.total(), "margin", shutdownMargin)
	}
	return plan, grace
}
