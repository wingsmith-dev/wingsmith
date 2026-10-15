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

Authentication is limited to the shape ADR 0016 describes: which tenant a request belongs to, and
on the public listener whether it proved it with the tenant's key. There is no user identity below
the tenant; a tenant's own auth for its own users happens in front of this hop.

## Tenancy and the two listeners

There is one binary, one routing table, and two listeners over it (docs/decisions/0016). Which one
accepted a connection is the one fact about a caller nothing it sends can forge, and it is what the
trust decision rests on — `internal/serving.Zone`. Every listener states its zone; there is no
default, so a listener cannot end up unauthenticated by omission.

- **The internal listener** (`-listen`, `:8080`) authenticates nobody. Reaching it is the proof; a
  tenant behind it may declare `auth: none`, and a caller there can reach every enabled tenant's
  endpoints. An operator's own edge is `internal` + `none`: it authenticates and bills its own users,
  then calls here.
- **The public listener** (`HANGAR_GATEWAY_PUBLIC_LISTEN`) sits behind a `*.hangar.example.com`
  wildcard. A request there is identified by its `Host` header, checked against a key when the tenant
  declares `auth: api_key`, and scoped to that tenant's own catalog and routing. Another tenant's
  model reads as `404`, never `403`. Only a tenant whose `ingress` is `public` is served here, and
  only its endpoints whose `visibility` is `public`; a `private` endpoint, the plane's default, is
  served on the internal listener alone.

**The internal listener needs a NetworkPolicy.** Its trust is the network boundary, and Kubernetes
has none by default: without a policy, any pod that can reach the gateway can call every tenant's
models without a key. `deploy/networkpolicy.yaml` is an example that admits the edge to `:8080`,
the ingress controller to the public listener, agents to the tunnel port and monitoring to
`:9090`, and denies everything else. Adapt its selectors and apply it with the gateway.

**A disabled tenant is refused on both listeners.** The gateway checks the tenant table itself
rather than trusting the routing table to have dropped the tenant's endpoints, because the two
refresh separately. What the caller sees depends on how the plane reports the tenant:

- listed in the tenant feed with `enabled: false`: `403 tenant_disabled` on both listeners. On the
  public listener the check runs after the key check, so an unauthenticated caller does not learn
  whether a tenant is enabled.
- left out of the feed: on the public listener its host is unknown, so `404 unknown_host`. On the
  internal listener, an endpoint whose owner is missing makes the gateway reload the tenant table
  (at most once every 2 seconds, however many requests miss) and look again. Still missing means
  `403 tenant_disabled`; a reload that failed means `503 tenant_unverified` with `Retry-After`. The
  reload is what keeps a tenant created moments ago from being refused before the next resync.

A tenant created moments ago can still get `404 unknown_host` on the public listener until the
next resync (`-resync`, 30s by default). The public listener does not reload on a miss, because any
caller on the internet can send an unknown `Host`.

## Configuration

| Flag / variable | What |
|---|---|
| `-listen` | listen address, default `:8080` |
| `-plane-url` / `HANGAR_PLANE_URL` | the plane's base URL |
| `HANGAR_GATEWAY_SERVICE_TOKEN` | the gateway's service credential for the plane, required. The plane accepts it on `GET /v1/endpoints/resolved`, `GET /v1/gateway/tenants` and `POST /v1/clusters/tunnel-auth` only. A 401 or 403 on it is logged as a gateway configuration error and never read as a verdict on an agent's enrolment token |
| `-resync` | how often to re-read the routing table and the tenant table, default 30s |
| `HANGAR_GATEWAY_PUBLIC_LISTEN` | address for the public, tenant-identified listener; unset (the default) means off — env only, never a flag, for the reason below |
| `-credentials` / `HANGAR_GATEWAY_CREDENTIALS` | JSON file of `{"<credential id>": {"<header>": "<value>"}}` |
| `-metrics-listen` / `HANGAR_GATEWAY_METRICS_LISTEN` | where `/metrics` is served, default `:9090`, `off` to disable |
| `-probe-token` / `HANGAR_GATEWAY_PROBE_TOKEN` | what the conformance runner presents to mark its traffic as a probe; a comma-separated pair while rotating |
| `-tunnel-listen` / `HANGAR_GATEWAY_TUNNEL_LISTEN` | where cluster agents dial, default `:8443`, `off` to disable |
| `-gateways` / `HANGAR_GATEWAY_REPLICAS` | how many gateway replicas are running, told to every dialling agent; must equal the StatefulSet's `replicas`. See [Scaling](#scaling) |
| `-gateway-id` / `HANGAR_GATEWAY_ID` | this replica's identity, reported to agents; defaults to the hostname, which is the pod name in a StatefulSet |
| `HANGAR_GATEWAY_REDACT_NAMES` | comma-separated names your own infrastructure goes by, redacted from quoted engine messages on top of the built-in stack names; env only |
| `-tunnel-cert` / `-tunnel-key` (`HANGAR_GATEWAY_TUNNEL_TLS_CERT` / `_KEY`) | PEM pair for the tunnel listener; neither means plaintext, and TLS has to be terminated in front of it |
| `HANGAR_GATEWAY_TENANT_MAX_IN_FLIGHT` | requests one tenant may have in flight on one replica, default `1024`, `0` for no cap |
| `HANGAR_GATEWAY_TENANT_RPS` / `HANGAR_GATEWAY_TENANT_BURST` | sustained requests per second one tenant may send to one replica, and the burst above it; unset means no rate limit |
| `HANGAR_GATEWAY_PUBLIC_ALLOW_NO_AUTH` | `true` serves a public tenant declaring `auth: none`; off by default, so such a tenant gets `401` |
| `HANGAR_GATEWAY_MAX_BODY_BYTES` | the largest request body accepted, default 100 MiB; a larger one is `413` |
| `HANGAR_GATEWAY_MAX_BUFFERED_BYTES` | request-body memory held at once across the replica, default 1000 MiB, `0` for no bound; a request that would pass it is `503 no_capacity`. Each request is charged its body, the same again as working room for the stages that decode it, and the latest rewritten copy, so the limit is close to what is resident; leave the pod headroom above it for everything else |
| `HANGAR_GATEWAY_READ_HEADER_TIMEOUT` | how long a caller may take to send its headers, default `10s` |
| `HANGAR_GATEWAY_BODY_READ_TIMEOUT` | how long a caller may take to send its body once the headers are in, default `2m`; a stalled body is `408` |
| `HANGAR_GATEWAY_WRITE_IDLE_TIMEOUT` | how long one write to the caller may block, default `2m`. Per write, so a long stream that keeps arriving is never cut; a caller that stops reading is |
| `HANGAR_GATEWAY_MEDIA_URL_HOSTS` | comma-separated hosts (`media.example.com`, `*.cdn.example.net`) a request's remote media may come from; unset allows public hosts and refuses internal ones. See [Remote media](#remote-media) |
| `HANGAR_GATEWAY_SHUTDOWN_GRACE` | the pod's `terminationGracePeriodSeconds`, default `30s`; the drain phases are cut from it, see [Shutdown](#shutdown) |
| `HANGAR_GATEWAY_DRAIN_DELAY` / `HANGAR_GATEWAY_DRAIN_TIMEOUT` / `HANGAR_GATEWAY_TUNNEL_DRAIN_TIMEOUT` | override one shutdown phase; at the default grace they are `5s` / `16s` / `5s` |
| `-log-upstream-bodies` / `HANGAR_GATEWAY_LOG_UPSTREAM_BODIES` | write a discarded upstream error body to the log whole. **Off in production and must stay off**: an engine's error quotes the request it refused. A debugging switch for a cluster carrying no customer traffic; it warns on every start while on |

Only the plane URL and the service token are required; nothing else is fatal by its absence. That is
a deploy constraint rather than a courtesy: a manifest rolled out before the image it names reaches
the old binary first, and a flag that binary does not know makes `flag.Parse` exit 2. Prefer the
environment variable when adding one to a manifest — an unknown variable is inert, an unknown flag
is a crashloop of the data plane. `HANGAR_GATEWAY_PUBLIC_LISTEN` goes further and carries no flag at all, because a
manifest naming its address is exactly the change that turns direct tenants on.

The credential file is how a reference on a cluster's serving address becomes headers on the wire.
The plane stores the reference and never the secret, so the material is mounted next to the process
that redeems it.

## Limits

Each replica bounds every tenant it serves: at most `HANGAR_GATEWAY_TENANT_MAX_IN_FLIGHT` requests
in flight (default 1024), and, when `HANGAR_GATEWAY_TENANT_RPS` is set, a token-bucket request rate.
A tenant over either gets `429 rate_limited` with `Retry-After`, counted as the `rate_limited`
outcome rather than as a failure of this hop. On the public listener the tenant is the host's and
the check runs before the body is read; on the internal listener it is the endpoint's owner, which
is known only once the body names a model. The limits are per replica, so the fleet-wide figure is
the per-replica value times the replica count.

On the internal listener an operator's edge is usually one tenant carrying every customer, so size
the in-flight cap to the edge's own concurrency, or set it to `0` there.

These are protective limits. The serving quota an endpoint sells, which the plane publishes on the
routing table as `limits`, is not enforced by the gateway yet.

## Remote media

A multimodal request can name its image, video or audio by URL, and the engine fetches it from
inside the serving cluster, from a GPU node, with whatever that network can reach. The gateway
checks every remote media URL before the request leaves this hop and refuses one it does not allow
with `400 media_url_not_allowed`. `data:` URLs carry bytes and always pass.

- **By default** (`HANGAR_GATEWAY_MEDIA_URL_HOSTS` unset) a URL must be `http` or `https` and must not
  name a private or cluster-internal host: loopback, private, link-local (including the cloud
  metadata address), CGNAT and unspecified addresses, numeric host forms such as `127.1`,
  single-label names such as `hangar-plane`, and names under `.local`, `.localhost`, `.internal`,
  `.svc`, `.cluster.local`, `.lan` and `.home.arpa`. Every public host is allowed.
- **With `HANGAR_GATEWAY_MEDIA_URL_HOSTS`** a URL must name a host on that list, and nothing else
  passes. Listing a private host allows it.

What is checked is where an engine fetches from: chat `image_url` / `video_url` / `audio_url` parts,
Responses `input_image` and `input_file`, Anthropic `image` and `document` blocks with a `url`
source, the same parts nested in tool results and function call outputs, and on `/v1/images/edits`
and `/v1/videos` any top-level field other than the prompt written as a URL. A citation, a web
search result or a tool schema in a conversation's history is not fetched by anything and is not
checked. Multipart bodies carry their media as bytes and are not walked.

The check is by name, so a public name that resolves to a private address gets through. It does not
replace the engine's own control (vLLM's `--allowed-media-domains`) or an egress NetworkPolicy on
the worker namespace, which also cover traffic that did not come through this gateway. Use all three
where tenants can send media URLs.

## Scaling

Agents dial out to the gateway, and every replica needs a tunnel from every cluster it routes to.
An agent learns how many replicas there are from the number each replica reports on the handshake,
and keeps redialling the tunnel address until it has a tunnel to that many distinct gateway ids.
The gateway does not discover that number: it has no Kubernetes client, so it is told with
`HANGAR_GATEWAY_REPLICAS`, and that value must equal the StatefulSet's `replicas`.

- **Set too low**, agents stop dialling before they cover every replica. The replicas they missed
  hold no tunnel for those clusters and answer `503 no_capacity` for their models while reporting
  healthy. This is the failure to avoid.
- **Set too high**, agents keep redialling and land on replicas they already hold. It costs some
  dials and nothing else.
- **Unset**, every replica reports one, which is only right for a single replica. The gateway logs a
  warning at startup.

Change both numbers in the same rollout. The pods restart, every agent redials, and each reads the
new count. Readiness (`/readyz`) only fails while a replica can reach none of the clusters in its
routing table, so a replica missing one cluster's tunnel stays in the Service and answers `503` for
that cluster's models; `hangar_gateway_tunnel_coverage` shows which replica lacks which cluster.

## Shutdown

On `SIGTERM` the replica reports not-ready, keeps accepting for `HANGAR_GATEWAY_DRAIN_DELAY` while
endpoint removal reaches every load balancer, then gives the internal and public listeners each the
whole `HANGAR_GATEWAY_DRAIN_TIMEOUT` to finish what they are serving, concurrently. Whatever is still
running when that ends is cut. The tunnel listener is drained after them, under its own
`HANGAR_GATEWAY_TUNNEL_DRAIN_TIMEOUT`, because the requests being drained ride those tunnels.

The phases are cut from `HANGAR_GATEWAY_SHUTDOWN_GRACE`, which must equal the pod's
`terminationGracePeriodSeconds`: up to 5 seconds each for the delay and the tunnel drain, 1 second
for the metrics listener, 3 seconds left unspent before the kubelet's `SIGKILL`, and the rest to the
request listeners. At the default 30 seconds that is `5s` / `16s` / `5s`, ending at 27 seconds. A
phase set explicitly is used as given, and the gateway logs a warning at startup when the phases
together outlast the grace period.

That budget bounds how long a generation can survive a rollout. To carry longer generations through
one, raise `terminationGracePeriodSeconds` and `HANGAR_GATEWAY_SHUTDOWN_GRACE` together; the extra
time goes to the request listeners. The agent's own drain on the cluster side is separate and needs
the same treatment.

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

A request carrying media still has the prompt identity folded in on top (`cachesalt.go`), so two
different images of the same size cannot share KV blocks inside a tenant. A caller's own `cache_salt` is folded in,
never honoured as it stands: it can narrow its own sharing and can never name another tenant's.
`x-tenant-id`, which Dynamo reads as a salt that overrides the body's, is stripped with the other
steering headers. An endpoint with no owning tenant on the internal listener is left as it was.

**What the media salt costs.** It works around an engine defect, not a gateway choice: on a
disaggregated deployment the decode worker receives a multimodal prompt as token ids with the media
dropped, so its prefix-cache hash cannot tell two images of the same size apart, and two prompts
that differ only after the image collide too. The only identity the engine cannot contradict is the
whole prompt, so every request carrying media gets a salt of its own. A repeat of the exact same
request still reuses its cache; anything else does not. In practice **a multi-turn conversation that
carries media gets no prefix-cache reuse from one turn to the next**, and every turn pays full
prefill for the whole conversation, images included. Text-only requests are unaffected. Any
prefix-cache hit rate or time-to-first-token figure quoted for an endpoint holds for text traffic
only. The salt can be narrowed back to the media identity once the engine carries multimodal
identity across the prefill/decode handoff.

The salt is spliced into the bytes that arrived rather than written by re-encoding the body: a text
request's `nvext` is the only part decoded (`splicedCacheSalt`). Re-encoding cost about 31 ms on a
4 MB prompt and splicing about 5 ms, most of it the media check every request already paid
(`BenchmarkCacheSaltLargeText`). A request carrying media, or a repeated `nvext`, still takes the
full re-encode. The served-name rewrite is spliced the same way. The endpoint's policy terms and the
media-decode strip still re-encode the top level, but only for a request they actually change.

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

`hangar_gateway_upstream_replays_total` counts request bodies sent to a cluster a second time
because the transport retried them, usually when a tunnel or connection went away before the
request was processed. `hangar_gateway_build_info{version}` names the running build. Per-tunnel
stream use and the agent's own request metrics are not exported yet.

The gateway does not start or record traces. A caller's `traceparent` and `tracestate` are forwarded
to the cluster untouched, so a trace started at the edge continues into the serving runtime.

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
- **A caller's own credentials.** `Authorization`, `Proxy-Authorization`, `Cookie`, `X-Api-Key`,
  `X-Hangar-Probe-Token`, `X-Hangar-Origin` and `X-Hangar-Cache-Scope` are removed before the
  request leaves. A tenant's key is redeemed here and no cluster, agent or engine log gets a copy.
  The credential a serving address sits behind is written after the removal, so it still goes out.
- **A response header from behind this hop.** Deleted unless it is on the forwarded list in
  `errors.go`. An allowlist, because what a caller needs is small and closed while what an engine
  might add is neither. A `Location` is reduced to a path first: the authority a frontend builds one
  from is the address the agent dialled.
- **A path that is not on the list.** Only a `POST` to `/v1/chat/completions`, `/v1/completions`,
  `/v1/responses`, `/v1/messages`, `/v1/embeddings`, `/v1/images/generations`, `/v1/images/edits`,
  `/v1/videos`, `/v1/videos/stream`, `/v1/audio/transcriptions`, `/v1/audio/translations` or
  `/v1/audio/speech` is forwarded (`forwardedPaths` in `modality.go`), plus `GET /v1/models`, which
  is answered here. Anything else is `404 unsupported_path` before the body is read: an engine
  frontend serves admin, debug and metrics routes too, and none of them are a caller's to reach.
  That includes vLLM's `/tokenize`, which `vllm bench serve` calls; through the gateway that call
  gets a 404 and the benchmark carries on with a warning.
  Any non-`POST` to `/v1/videos` or `/v1/videos/*` is a `404 unsupported_path` saying generation is
  synchronous: Dynamo 1.5.0 has no video job store, and `POST /v1/videos` returns the finished video.
- **A request with no body.** Not parsed as JSON. A bodiless `POST` is `400 missing_model`.
- **`X-Request-Id`.** Every response carries one, including the errors this hop writes itself. A
  caller's own value is kept when it is at most 128 characters of `[A-Za-z0-9._:-]`; otherwise a
  UUIDv4 is minted. The same id is the `request_id` on the `gateway: request` access line and on the
  upstream-error lines, and it is forwarded to the cluster, where Dynamo's frontend attaches
  `x-request-id` to its own trace context. An upstream's own `X-Request-Id` never replaces it.
