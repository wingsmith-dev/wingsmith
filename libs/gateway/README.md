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
  ingress. Reaching it at all is the proof; a tenant behind it may declare `auth: none`. An operator's
  own edge is `internal` + `none` — it authenticates and bills its own users, then calls here, and
  nothing about that changed.
- **The public listener** (`HANGAR_GATEWAY_PUBLIC_LISTEN`) sits behind a `*.hangar.example.com` wildcard. A
  request there is identified by its `Host` header, checked against a key when the tenant declares
  `auth: api_key`, and scoped to that tenant's own catalog and routing — another tenant's model reads
  as `404`, never `403`. This is how a tenant reaches the gateway directly instead of through
  the operator's own inference edge.

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
| `HANGAR_GATEWAY_REDACT_NAMES` | comma-separated names your own infrastructure goes by, redacted from quoted engine messages on top of the built-in stack names; env only |
| `-tunnel-cert` / `-tunnel-key` (`HANGAR_GATEWAY_TUNNEL_TLS_CERT` / `_KEY`) | PEM pair for the tunnel listener; neither means plaintext, and TLS has to be terminated in front of it |
| `-log-upstream-bodies` / `HANGAR_GATEWAY_LOG_UPSTREAM_BODIES` | write a discarded upstream error body to the log whole. **Off in production and must stay off**: an engine's error quotes the request it refused. A debugging switch for a cluster carrying no customer traffic; it warns on every start while on |

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

## What it never keeps

Request and response content is not retained by this hop, and that is a promise made to buyers, so
it is held in code rather than in a log pipeline's configuration.

- **The access line** (`gateway: request`) carries the request id, method, path (never the query),
  status, outcome, timings, token counts, the tenant and a 12-character key-hash prefix. Never a
  body, a header value or a model's answer.
- **A discarded upstream error** is logged as a content-free summary: status, the engine's own
  `type`/`code` when they parse as short identifiers, the body's length and whether it was
  truncated. An engine's validation message quotes the input it rejected and a 5xx trace can carry
  the request it was handling, so the body itself reaches the log only under
  `-log-upstream-bodies`, which is off in production.
- **Engine logging** is the operator's half: `--enable-log-requests`, `--enable-log-outputs`,
  `--log-requests` and `--log-requests-level` are refused by the plane on a workload, a role and an
  overlay, and stripped by the operator's renderer under any spelling. So is any log level at which
  an engine writes request content — SGLang's `--log-level debug`, `DYN_LOG` with a `debug` or
  `trace` directive, `VLLM_LOGGING_LEVEL=DEBUG`, `VLLM_DEBUG_LOG_API_SERVER_RESPONSE` and
  `DYN_LOGGING_CONFIG_PATH` — refused in the plane and dropped from the rendered graph after the
  overlays merge (`libs/operator/internal/render/retention.go`).

## Prefix-cache isolation

Dynamo folds `nvext.cache_salt` into every KV block hash (and, since 1.4, into the router's overlap
score), so the salt decides whose prefix cache a request may reuse. Unsalted, two tenants on one
deployment share a cache, and time to first token tells one of them what the other sent recently.

Every text generation request (`/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
`/v1/messages`) on a text endpoint is salted with its tenant:

- on the public listener, the tenant the `Host` resolved;
- on the internal listener, the tenant that owns the endpoint, narrowed by `X-Hangar-Cache-Scope`
  when the internal caller sends one. A platform serving many customers through one tenant names the
  customer there, and gets one cache per customer. The header is ignored on the public listener and
  removed before the request leaves this hop.

A request carrying media still has the prompt identity folded in on top (`cachesalt.go`), so the
2026-09-12 image collision stays closed inside a tenant. A caller's own `cache_salt` is folded in,
never honoured as it stands: it can narrow its own sharing and can never name another tenant's.
`x-tenant-id`, which Dynamo reads as a salt that overrides the body's, is stripped with the other
steering headers. An endpoint with no owning tenant on the internal listener is left as it was.

The salt is spliced into the bytes that arrived rather than written by re-encoding the body: a text
request's `nvext` is the only part decoded (`splicedCacheSalt`). Re-encoding cost about 31 ms on a
4 MB prompt and splicing about 5 ms, most of it the media check every request already paid
(`BenchmarkCacheSaltLargeText`). A request carrying media, or a repeated `nvext`, still takes the
full re-encode.

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

Text usage is the upstream's own `usage` object, read from a 64 KiB tail of the response and
written as `input_tokens`/`output_tokens` on the `gateway: request` line and
`hangar_gateway_tokens_total`. Image and video generations have none — Dynamo 1.5.0's image and
video responses carry no usage the worker could fill — so this hop records raw facts for a
successful `/v1/images/generations`, `/v1/images/edits`, `/v1/videos`, or `/v1/chat/completions`
answered by an image model, on the same line and in `hangar_gateway_generated_total{unit}`:

- `images` / `videos`: outputs counted as the response streams past (each `b64_json` or `url` in
  `data`, or each `"type":"image_url"` part of a chat answer). Nothing is buffered beyond the tail.
- `size` with `size_source=request|response`: the request's `size`, else the image response's own
  `size` field. No default is assumed, because the engine may apply its own.
- `video_seconds`, `video_frames`: as the request stated them — `seconds` (string or number), or
  `nvext.num_frames / nvext.fps`, with frames as `seconds * fps` when only those are given.
- `upstream_ms` (`upstream_seconds` in the series): wall time from the forward to the last response
  byte, the nearest this hop comes to GPU time; `inference_time_s` beside it when the response
  carried one (Dynamo's video response does).

No pricing, and nothing is added to the response body the caller sees.

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
- **An upstream answers with an error.** The body is replaced with one of this hop's own objects,
  keyed on the upstream's status; the `type` and `code` are always ours. A caller's `4xx` keeps its
  status (`422` becomes `400`) and a `5xx` becomes `502 upstream_error`.
  **A `4xx` engine message is passed through, sanitised.** When the body is a JSON error
  (`{"message"}`, `{"error":{"message"}}`, `{"error":"..."}`, `{"detail"}`), its message is cut to
  its first line (no tracebacks), a leading exception class is dropped, URLs, cluster-internal
  hostnames, IP addresses, source files, filesystem paths and any token naming the stack (the
  conformance leak needles, plus `HANGAR_GATEWAY_REDACT_NAMES`) become `[redacted]`, the served model name reads back as the caller's,
  and it is capped at 500 bytes. This is how input errors reach a caller: the vllm-omni layer
  raises `ValueError` for a validation failure, and Dynamo's frontend answers that `400`
  `InvalidArgument` with the message. A `4xx` prose body is never quoted, and one that mentions a
  known reason, such as `invalid video file`, is answered with a fixed message from this hop's own
  table. **A `5xx` is never quoted**: the caller gets the fixed sentence and nothing the engine
  said. The full body is logged with the request id either way.
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
- **A request with no body.** Not parsed as JSON. A bodiless `GET`, `HEAD` or `DELETE` has no
  `model` to route on and is a `404 unsupported_path`; a bodiless `POST` is `400 missing_model`.
  Any non-`POST` to `/v1/videos` or `/v1/videos/*` is a `404 unsupported_path` saying generation is
  synchronous: Dynamo 1.5.0 has no video job store, and `POST /v1/videos` returns the finished video.
- **`X-Request-Id`.** Every response carries one, including the errors this hop writes itself. A
  caller's own value is kept when it is at most 128 characters of `[A-Za-z0-9._:-]`; otherwise a
  UUIDv4 is minted. The same id is the `request_id` on the `gateway: request` access line and on the
  upstream-error lines, and it is forwarded to the cluster, where Dynamo's frontend attaches
  `x-request-id` to its own trace context. An upstream's own `X-Request-Id` never replaces it.
