package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"os"
	"strings"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/packages/tunnel"
)

// tunnelSettings are the tunnel listener and pool limits read from the
// environment. A zero field leaves the tunnel package's default, and -1 turns
// off a limit that can be turned off.
//
//	WINGSMITH_GATEWAY_TUNNEL_MAX_PER_CLUSTER             tunnels one cluster may hold on a replica
//	WINGSMITH_GATEWAY_TUNNEL_MAX                         tunnels a replica holds across every cluster
//	WINGSMITH_GATEWAY_TUNNEL_BODY_IDLE_TIMEOUT           a response body unread this long is closed; "off" disables
//	WINGSMITH_GATEWAY_TUNNEL_CACHE_TTL                   how long a verified token is reused without asking the plane
//	WINGSMITH_GATEWAY_TUNNEL_CACHE_GRACE                 how long a verified token survives a plane that cannot be reached
//	WINGSMITH_GATEWAY_TUNNEL_REVERIFY_INTERVAL           how often an open tunnel's token is checked again
//	WINGSMITH_GATEWAY_TUNNEL_MAX_HANDSHAKES              dials mid-handshake at once
//	WINGSMITH_GATEWAY_TUNNEL_MAX_HANDSHAKES_PER_SOURCE   of those, from one address; "off" disables
//	WINGSMITH_GATEWAY_TUNNEL_SOURCE_FAILURES             failed dials per address per minute before it is throttled; "off" disables
//
// The two per-source limits key on the dial's address, so they only work when
// the tunnel Service preserves it (externalTrafficPolicy: Local, or a
// pass-through load balancer). Behind one that rewrites it, set both to "off".
// Re-verification cannot be turned off from here.
type tunnelSettings struct {
	maxPerCluster          int
	maxTunnels             int
	bodyIdle               time.Duration
	cacheTTL               time.Duration
	cacheGrace             time.Duration
	reverify               time.Duration
	maxHandshakes          int
	maxHandshakesPerSource int
	sourceFailures         int
}

func tunnelSettingsFromEnv() tunnelSettings {
	return tunnelSettings{
		maxPerCluster:          envInt("WINGSMITH_GATEWAY_TUNNEL_MAX_PER_CLUSTER"),
		maxTunnels:             envInt("WINGSMITH_GATEWAY_TUNNEL_MAX"),
		bodyIdle:               envDurationOrOff("WINGSMITH_GATEWAY_TUNNEL_BODY_IDLE_TIMEOUT"),
		cacheTTL:               envDuration("WINGSMITH_GATEWAY_TUNNEL_CACHE_TTL"),
		cacheGrace:             envDuration("WINGSMITH_GATEWAY_TUNNEL_CACHE_GRACE"),
		reverify:               envDuration("WINGSMITH_GATEWAY_TUNNEL_REVERIFY_INTERVAL"),
		maxHandshakes:          envInt("WINGSMITH_GATEWAY_TUNNEL_MAX_HANDSHAKES"),
		maxHandshakesPerSource: envIntOrOff("WINGSMITH_GATEWAY_TUNNEL_MAX_HANDSHAKES_PER_SOURCE"),
		sourceFailures:         envIntOrOff("WINGSMITH_GATEWAY_TUNNEL_SOURCE_FAILURES"),
	}
}

func (s tunnelSettings) pool(cfg tunnel.PoolConfig) tunnel.PoolConfig {
	cfg.MaxTunnelsPerCluster = s.maxPerCluster
	cfg.MaxTunnels = s.maxTunnels
	cfg.BodyIdleTimeout = s.bodyIdle
	return cfg
}

func (s tunnelSettings) listener(cfg tunnel.ListenerConfig) tunnel.ListenerConfig {
	cfg.CacheTTL = s.cacheTTL
	cfg.CacheGrace = s.cacheGrace
	cfg.ReverifyInterval = s.reverify
	cfg.MaxHandshakes = s.maxHandshakes
	cfg.MaxHandshakesPerSource = s.maxHandshakesPerSource
	cfg.SourceFailures = s.sourceFailures
	return cfg
}

// envIntOrOff is envInt that also reads "off" as -1.
func envIntOrOff(key string) int {
	if strings.EqualFold(strings.TrimSpace(os.Getenv(key)), "off") {
		return -1
	}
	return envInt(key)
}

// envDurationOrOff is envDuration that also reads "off" as -1.
func envDurationOrOff(key string) time.Duration {
	if strings.EqualFold(strings.TrimSpace(os.Getenv(key)), "off") {
		return -1
	}
	return envDuration(key)
}

// rebindBackoff bounds how often a tunnel listener that keeps failing is bound
// again.
const (
	rebindBackoffMin = time.Second
	rebindBackoffMax = 30 * time.Second
)

// keepServing runs the tunnel listener until ctx ends or the listener is shut
// down.
//
// Serve waits out temporary accept errors itself. One it returns is a socket
// that has gone bad, and the answer is a new socket, not a new process: the
// tunnels already open do not depend on the accept socket, and exiting would
// take every one of them down to recover a port that only new dials need.
func keepServing(ctx context.Context, listener *tunnel.Listener, ln net.Listener, rebind func() (net.Listener, error)) {
	backoff := rebindBackoffMin
	for {
		started := time.Now()
		err := listener.Serve(ctx, ln)
		if err == nil || errors.Is(err, tunnel.ErrListenerClosed) || ctx.Err() != nil {
			return
		}
		_ = ln.Close()
		if time.Since(started) > time.Minute {
			backoff = rebindBackoffMin
		}
		slog.Error("gateway: tunnel listener stopped accepting, binding it again; open tunnels are unaffected",
			"err", err, "retry_in", backoff)
		for {
			if !sleepCtx(ctx, backoff) {
				return
			}
			backoff = min(backoff*2, rebindBackoffMax)
			next, err := rebind()
			if err == nil {
				ln = next
				break
			}
			slog.Error("gateway: could not bind the tunnel listener again", "err", err, "retry_in", backoff)
		}
	}
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
