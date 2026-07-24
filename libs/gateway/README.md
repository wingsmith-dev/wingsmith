# gateway

Hangar's data plane. One hop: it takes an OpenAI-compatible request, reads the `model` out of the
body, and forwards it to a cluster currently serving that model.

It is the only part of hangar that sits in the request path, and it holds nothing the control plane
holds. The plane derives the routing table; the gateway keeps a copy in memory and refreshes it on
a timer. Nothing is joined per request.

It is also the gateway end of the tunnel. There is no separate gateway deployment: one
StatefulSet, `hangar-gateway`, answers callers on one port and accepts cluster dials on another. A
cluster runs two hangar processes and neither is this one — the operator, which operates the
cluster, and the agent, which dials out to the gateway and carries inference requests in.

## Layout

| Path | What it owns |
|---|---|
| `cmd/gateway/` | the binary: flags, listeners, shutdown |
| `internal/serving/` | the HTTP entry point — mux, reverse proxy, the tenant and key stages, per-request observation, readiness, usage |
| `internal/routing/` | the model-to-cluster table and upstream selection |
| `internal/tenancy/` | the host-to-tenant and hash-to-tenant table |
| `internal/plane/` | client for the plane's HTTP API |
| `internal/metrics/` | the Prometheus registry |
| `../packages/tunnel/` | the wire, its own module, both ends of it |
| `../agent/` | the cluster end, its own module, its own upgrade cadence |

## What it does not do

- **No metering or billing.** Whoever puts a gateway in front of their users already has a place
  where that happens. Two systems counting tokens is two systems disagreeing about an invoice.
- **No replica selection.** The serving runtime inside a cluster routes on live queue depth and
  KV-cache residency. That state does not leave the cluster and should not.
- **No catalog.** `GET /v1/models` lists names, not context lengths or quantization. Describing a
  model is the job of whatever is selling it.

This used to say "no authentication" too. It is reversed for authentication only, and only in the
shape docs/plans/17 describes: which tenant a request belongs to, and whether it proved it. There is
still no metering, no billing, and no user identity below the tenant — a tenant's own auth for its
own users happens in front of this hop, exactly as it always has.

## Tenancy and the two listeners

There is one binary, one routing table, and two listeners over it (docs/plans/17). Which one accepted
a connection is the one fact about a caller nothing it sends can forge, and it is what the trust
decision rests on — `internal/serving.Zone`.

- **The internal listener** (`-listen` / `HANGAR_GATEWAY_PUBLIC_LISTEN` unset) is a ClusterIP with no
  ingress. Reaching it at all is the proof; a tenant behind it may declare `auth: none`. Acme's
  own edge is `internal` + `none` — it authenticates and bills its own users, then calls here, and
  nothing about that changed.
- **The public listener** (`HANGAR_GATEWAY_PUBLIC_LISTEN`) sits behind a `*.hangar.example.com` wildcard. A
  request there is identified by its `Host` header, checked against a key when the tenant declares
  `auth: api_key`, and scoped to that tenant's own catalog and routing — another tenant's model reads
  as `404`, never `403`. This is how a tenant reaches the gateway directly instead of through
  Acme's inference edge.

`HANGAR_GATEWAY_TOKEN` and the shared-secret guard it configured are gone. Their job — is this hop
being called by the system that is supposed to call it — is answered by which listener a caller
reached, which is a stronger claim than a bearer token ever was: it cannot be copied out of one
caller's config and pasted into another's.

## Configuration

| Flag / variable | What |
|---|---|
| `-listen` | listen address, default `:8080` |
| `-plane-url` / `HANGAR_PLANE_URL` | the plane's base URL |
| `HANGAR_OPERATOR_TOKEN` | how it reads the plane |
| `-resync` | how often to re-read the routing table and the tenant table, default 30s |
| `HANGAR_GATEWAY_PUBLIC_LISTEN` | address for the public, tenant-identified listener; unset (the default) means off — env only, never a flag, for the reason below |
| `-credentials` / `HANGAR_GATEWAY_CREDENTIALS` | JSON file of `{"<credential id>": {"<header>": "<value>"}}` |
| `-metrics-listen` / `HANGAR_GATEWAY_METRICS_LISTEN` | where `/metrics` is served, default `:9090`, `off` to disable |
| `-probe-token` / `HANGAR_GATEWAY_PROBE_TOKEN` | what the conformance runner presents to mark its traffic as a probe; a comma-separated pair while rotating |
| `-tunnel-listen` / `HANGAR_GATEWAY_TUNNEL_LISTEN` | where cluster agents dial, default `:8443`, `off` to disable |
| `-tunnel-cert` / `-tunnel-key` (`HANGAR_GATEWAY_TUNNEL_TLS_CERT` / `_KEY`) | PEM pair for the tunnel listener; neither means plaintext, and TLS has to be terminated in front of it |

None of the last three is required and none is fatal by its absence, which is a deploy constraint
rather than a courtesy: `just deploy plane` runs `kubectl apply -k infra/`, and `infra/` carries this
component's manifest, so a manifest naming a flag the running image does not know reaches the old
binary and `flag.Parse` exits 2. Prefer the environment variable when adding one to a manifest — an
unknown variable is inert, an unknown flag is a crashloop of the data plane caused by deploying the
control plane. `HANGAR_GATEWAY_PUBLIC_LISTEN` goes further and carries no flag at all, because a
manifest naming its address is exactly the change that turns docs/plans/17's rollout on.

The credential file is how a reference on a cluster's serving address becomes headers on the wire.
The plane stores the reference and never the secret, so the material is mounted next to the process
that redeems it.

## What it measures

A request that fails before it reaches a cluster emits nothing a serving runtime can count. A model
nobody serves, an endpoint with nowhere to route, a credential that has gone stale, a cluster that
cannot be dialled — those are the requests that dominate an availability incident and none of them
are visible from inside a cluster. This hop sees every request, including the ones that never land,
so it is the only honest source of a failure rate.

`/metrics` is Prometheus text on its own listener, `:9090` by default. It is observability and not a
ledger: nothing here is metered or billed, and no caller identity, API key, client address or raw
model string reaches a label. Those are unbounded, and an unbounded label is the same mistake as a
shared counter in the request path.

Every request is counted under one of ten outcomes, and the set is closed — an operator who can
redefine failure produces an availability number that is not comparable between two deployments.
What is declared elsewhere is which outcomes sit in the numerator and the denominator of a named
objective. The distinction the whole endpoint turns on is that `rate_limited` and `shed` are both
`429`: one is a caller over its own declared limit and is excused, the other is this hop refusing
work and is a failure, and keying a failure rate off `status="429"` turns every capacity incident
into a clean bill of health.

`GET /healthz` is not counted — a kubelet probe is nobody's availability. `GET /v1/models` is
counted under the reserved endpoint `_catalog`, and a request naming a model we do not serve under
`_unknown` rather than under the string it asked for.

`hangar_gateway_routing_table_age_seconds` is seconds since the plane last confirmed the loaded
table, by sending a new one or by answering `304`. A failed refresh deliberately does not reset it:
the gateway keeps serving what it has, and that gauge climbing beside
`hangar_gateway_routing_table_refresh_success` at 0 is exactly the shape of a plane outage seen
from the data plane.

## Failure behaviour

- **The plane is down.** The table already loaded keeps serving. What it is built from changes on
  the order of days; a control-plane outage must not stop traffic to models that are running fine.
- **Boot with no table.** Fatal. Coming up empty would answer everything with `model_not_found`,
  which reads as an outage of the models rather than the misconfiguration it is.
- **One serving address is unusable.** Skipped, with the rest of the refresh applied. One bad URL
  must not take every other model down with it.
- **An endpoint with no upstreams.** `503 no_capacity`, never `404`. "No such model" and "this
  model has nowhere to go" are different answers and a caller acts differently on each.
- **A deployment the plane reports as not running.** Deprioritised, not excluded. Readiness is
  observed and can lag; refusing to route to a model that is in fact serving is the worse failure.
- **An upstream answers with an error.** The body is discarded and replaced with one of this hop's
  own objects, keyed on the upstream's status. Never forwarded, never wrapped, never passed through
  because it was already the right shape. An engine's message names the infrastructure that produced
  it — a path, a served name, a ClusterIP, a framework's reason phrase — and the set of errors a
  caller sees has to be a property of what was sold rather than of which engine build is running.
  The engine's own body is logged here and nowhere else. A `4xx` body that mentions a known reason,
  such as `invalid video file`, is answered with a fixed message from this hop's own table for that
  reason: the body selects the message and is never quoted.
- **An upstream answers `401`, `403` or `404`.** `502`, not the status that arrived. The credential is
  this hop's and the caller has never seen it, and a path that was routed here is not one the engine
  gets to call missing — both are misconfigurations on our side and telling a caller otherwise sends
  them to rotate a working key. This counts a former `client_error` against `upstream_error`, which
  is the intended reading: it was our fault before and the number said otherwise.
- **A request header or `nvext` field that steers the serving runtime.** Deleted before the request
  leaves. Dynamo reads a worker pin, a data-parallel rank, a queue priority, a queue class, a request
  id and a session binding from a caller's headers — every `x-dynamo-*`, the unprefixed worker and
  rank aliases, and the agent harness session headers — and from `nvext`. Which worker runs a
  request, and ahead of whom, is not a caller's to choose. A request carrying none of them is
  forwarded as the bytes that arrived. The names are in `steering.go`.
- **A response header from behind this hop.** Deleted unless it is on the forwarded list in
  `errors.go`. An allowlist, because what a caller needs is small and closed while what an engine
  might add is neither. A `Location` is reduced to a path first: the authority a frontend builds one
  from is the address the agent dialled.
