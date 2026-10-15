# 0018 — The inference tunnel is our own HTTP/2 role reversal, not an off-the-shelf tunnel

**Status:** accepted · 2026-10-15 (records a choice made when `libs/packages/tunnel` was written)

## Context

Nothing may dial into a GPU cluster (architecture §2), but the gateway has to send requests to a
model's frontend inside one. So the cluster has to dial out and the gateway has to send requests
back down that connection. This is a reverse tunnel, and several mature ones exist: frp, chisel,
cloudflared, ngrok, Teleport's reverse tunnel, and Kubernetes' Konnectivity
(apiserver-network-proxy).

What the gateway needs from the tunnel is narrower than "reach a port in the cluster", and the
off-the-shelf tools are built for that wider job:

1. **An `http.RoundTripper` per cluster.** The gateway is an `httputil.ReverseProxy`. It needs to
   pick a cluster per request and hand the request to something that speaks HTTP to that cluster,
   with streaming responses, per-request cancellation and a response-header timeout.
2. **Retry of unprocessed requests on a sibling connection.** An agent rollout drains its tunnels
   with a GOAWAY. Requests the agent never started must move to another live tunnel to the same
   cluster, not fail as 502s. A request that already streamed bytes to the caller must not be
   retried.
3. **A fast answer when a cluster is full**, instead of queueing behind a stream limit, and errors
   classified so that tunnel churn is not recorded as a cluster outage.
4. **Identity is the cluster's enrolment token.** The token the operator already holds, verified by
   the plane, decides which cluster a tunnel belongs to. A second credential system for the data
   path would be a second thing to mint, rotate and revoke per cluster.
5. **Coverage of every gateway replica from one address.** The tunnel pool is in-process, so each
   gateway replica can only serve clusters it holds a tunnel to. An agent given one address has to
   find all replicas, without the cluster's values listing them.
6. **No extra process or vendor in the request path.** The gateway is already the one Hangar
   process in the path. A separate tunnel server or a hosted edge adds a hop, a failure domain and,
   for a hosted edge, an account and a third party that sees every prompt.

## Options considered

| Option | What it gives | Why not |
|---|---|---|
| **frp** | Mature client/server reverse proxy with its own control protocol and multiplexing | A separate `frps` server in the path. Routing is by its own proxy configuration (ports or vhosts), not per request from Go. Auth is its own token or OIDC, not the enrolment token. Retry-on-GOAWAY and saturation answers would still have to be built in front of it. |
| **chisel** | TCP over SSH over WebSocket, reverse port-forwards | Hands the gateway a local port per remote, so the gateway would proxy to `localhost:N` and lose which cluster and which tunnel a request is on. SSH-style credentials, not the enrolment token. Multi-replica coverage unsolved. |
| **cloudflared / ngrok** | Hosted edge, no server to run | Ties a self-hosted data path to a vendor account and edge. The tunnel terminates at the vendor, not at the gateway, and every prompt crosses a third party. |
| **Konnectivity** | gRPC reverse tunnel built for the Kubernetes API server | The closest in shape, and the replica-discovery handshake follows the same pattern. Built around the API server's dial semantics; using it means carrying its server and gRPC framing for an HTTP-only path. |
| **Inbound load balancer per cluster** | No tunnel at all | Rejected by the architecture: it is the push model's reachability and credential problem moved to the data plane. |

## Decision

**Hangar carries its own tunnel, in `libs/packages/tunnel`, built on HTTP/2 with the roles reversed.**
The agent dials the gateway over TLS, presents its enrolment token in a single length-prefixed
handshake (`HGRTUN01`), and then runs the HTTP/2 *server* on that connection while the gateway runs
the HTTP/2 *client*. The gateway therefore holds an ordinary `http.RoundTripper` per cluster.

- The pool implements `http2.ClientConnPool`, so `http2.Transport`'s own retry runs and the pool
  hands it a different live tunnel. Connection resets that x/net does not retry are retried by the
  pool only before any byte has reached the caller.
- The response-header timeout is the upstream one, armed at request write and disarmed at the first
  header.
- A full cluster returns `ErrSaturated` at once instead of blocking for a stream slot.
- The handshake reply names the gateway replica and how many there are, so the agent redials one
  address until it holds a tunnel to each.
- The module depends on `golang.org/x/net/http2` and nothing else, and implements both ends, because
  neither the gateway nor the agent owns the protocol.

## Consequences

- **We own a security-relevant protocol.** It is about 5,500 lines including tests, and every
  change to it needs review as such. The threat model below is part of this record and changes with
  the code.
- **The protocol is versioned by its magic.** A wire change is a new magic, so an agent speaking the
  wrong version is rejected before anything is misread.
- **No extra hop or vendor.** The gateway terminates the tunnel itself; a gateway replica is also a
  tunnel server.
- **Replacing it later is contained.** The gateway sees a `RoundTripper` per cluster. Another tunnel
  that could provide one with the same retry and saturation behaviour would not change the gateway's
  routing.

## Threat model

**Assets.** Inference traffic (prompts and completions), each cluster's enrolment token, and the
mapping from a tunnel to a cluster.

**Boundaries.** The tunnel listener is reachable from wherever clusters run, usually the internet.
The agent runs inside a cluster the operator of the installation controls.

| Threat | Control | Residual risk |
|---|---|---|
| Eavesdropping or tampering between agent and gateway | TLS on the tunnel listener (`-tunnel-cert`/`-tunnel-key`), or TLS terminated by a TCP-passthrough load balancer in front; the agent verifies the gateway's certificate | A deployment that terminates TLS in front of the listener and forwards plaintext over an untrusted network; `HANGAR_TUNNEL_INSECURE_SKIP_VERIFY` is for local development only |
| Unauthenticated dials exhausting the gateway | 10 s handshake deadline, 8 KiB handshake cap, rejected dials counted by reason | Volumetric attacks on the listener are left to the network edge |
| A dial claiming to be another cluster | Identity is whatever the plane resolves the token to (`POST /v1/clusters/tunnel-auth`, called with the gateway's service token); a claimed cluster id that disagrees is rejected | None beyond token theft |
| A stolen enrolment token | Tokens are per cluster, so a stolen one impersonates that cluster only; rotating it in the plane refuses it on the next verification | Verifications are cached for 5 minutes, and for up to an hour while the plane is unreachable, so a revoked token can open new tunnels for that long. Tunnels already open stay open until they drop. |
| A compromised cluster | It can only attach tunnels for its own cluster, so it only receives traffic routed to models placed on it. HTTP/2 stream limits, flow-control windows and the response-header timeout bound what it can make the gateway hold | It sees the prompts sent to its own models, as any serving cluster does, and can answer them wrongly |
| Routing headers forged by a caller | The gateway overwrites `X-Hangar-Deployment-Id` on every request, and the agent trusts it only from a tunnel | None known |
| A compromised gateway | Out of scope for the tunnel: the gateway is in the request path by design and sees all traffic | Run it as trusted infrastructure |
