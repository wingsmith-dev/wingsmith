// Package tunnel carries inference requests into a GPU cluster over a
// connection the cluster dialled out.
//
// It is a standalone module and it implements *both* ends of the wire. The
// gateway end is Listener, Pool and Conn: it accepts dials, authenticates them,
// and hands the caller an http.RoundTripper per cluster. The cluster end is
// Connector: it serves HTTP/2 on a connection somebody else dialled. Neither
// binary owns the protocol, so it lives beside the other contracts in
// libs/packages instead of under one binary's internal/, which would have
// made one end the host and the other an importer of the host's module.
//
// The dependency rule follows from that: this module depends on http2 and
// nothing else. Not the routing table, not the metrics registry, not
// Kubernetes. A change to any of those must not be able to reach the wire
// format.
//
// An operator owns every cluster but not always the networks they sit in, so nothing may
// dial into one. The agent inside the cluster dials out to the gateway, and
// then the roles on that single connection are reversed: the agent runs the
// HTTP/2 *server* and the gateway runs the HTTP/2 *client*. The gateway
// therefore holds an ordinary http.RoundTripper per cluster and
// httputil.ReverseProxy drives it exactly as it drives a public address.
//
// The agent is not the operator. A cluster runs both: the operator is a
// leader-elected singleton on the control path, and the agent scales with
// traffic on the data path. They share neither a process nor a connection,
// because the data plane never crosses the control plane: if a
// request needed the operator's connection, control-plane uptime would become
// the inference SLA.
//
// # What role reversal costs, and how it is paid back
//
// A bare *http2.ClientConn is not a Transport, and four things that live on
// http2.Transport.RoundTrip are lost with it. Each is paid back here:
//
//   - Retry after GOAWAY. shouldRetryRequest is on Transport.RoundTrip, and its
//     sentinels are unexported. Pool is an http2.ClientConnPool, so the real
//     Transport.RoundTrip runs and the retry it owns is kept; the pool's job is
//     to hand the retry a *different* live tunnel to the same cluster. This is
//     why a cluster holds more than one. An agent that was killed instead
//     of drained never sends the GOAWAY, and the ECONNRESET, EPIPE or EOF that
//     arrives instead says nothing about whether the agent had already
//     forwarded the request. clusterTransport.RoundTrip resends such a request
//     only if its headers were never written, or if it is idempotent; a
//     completion the agent may have forwarded fails as churn instead of
//     running twice.
//   - ResponseHeaderTimeout. http2.Transport has no such field, but
//     ClientConn.responseHeaderTimeout reads it off the net/http Transport that
//     http2.ConfigureTransports attaches, so the pool is built through
//     ConfigureTransports, not by hand, and the timer is the upstream
//     one: armed at request write, disarmed at the first response header, and
//     never rearmed for the rest of a long generation.
//   - A fast answer when the connection is full. http2 blocks in
//     awaitOpenSlotForStreamLocked when a peer's MaxConcurrentStreams is
//     reached, which turns overload into unbounded latency. The pool calls
//     ClientConn.ReserveNewRequest instead and returns ErrSaturated when no
//     tunnel to the cluster has a slot.
//   - Error classification. http2.StreamError and http2.GoAwayError are neither
//     context.Canceled nor a net.Error timeout, so an agent redeploy would
//     otherwise be recorded as a cluster outage. Classify separates tunnel
//     churn from a cluster that genuinely failed.
//
// # Revoking a cluster's access
//
// The token behind every open tunnel is re-verified with the plane every
// ReverifyInterval, and a verified answer is cached for CacheTTL, so a token
// the plane stops accepting loses its tunnels within about the sum of the two:
// a little over two minutes with the defaults. For a leaked token, revoke or
// rotate it in the plane and either wait that long or restart the gateway
// replicas; a replica starts with an empty cache, so every agent redials and
// is verified afresh. Listener.Revoke closes one cluster's tunnels at once for
// an embedder that has its own trusted signal to call it from; the gateway in
// this repository does not expose it.
//
// A plane that cannot be reached never closes a tunnel; it only stops new
// dials whose token is not cached, after CacheGrace. That is only safe if an
// outsider cannot make the plane look unreachable. The verifier is handed the
// dial's source (DialSource) so the plane can charge failed guesses to the
// address that made them, not to the gateway, and a source that keeps
// failing is stopped at the listener before it reaches the plane at all.
//
// # Per-source limits
//
// MaxHandshakesPerSource and SourceFailures key on the dial's source address.
// They assume the listener sees the agent's own address. Behind a load
// balancer or Service that rewrites it, every agent shares a few node
// addresses and the limits would throttle them together; such a deployment
// should preserve the client address or set both limits negative.
//
// Reachability is measured here and nowhere else. Observed state has exactly
// one writer (ingestion, fed by the operator's sync stream), and the gateway
// must not become a second one.
package tunnel
