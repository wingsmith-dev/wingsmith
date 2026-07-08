# The HTTP API

What `admin` and `hgr` talk to. The plane ↔ operator link is a different contract entirely
([`PROTOCOL.md`](PROTOCOL.md)) and nothing here crosses it.

Two clients were built against invented shapes before this document existed, and they disagreed.
This resolves those disagreements; where a client diverges, the client is wrong.

---

## 1. Shape

Base path `/v1`. JSON in, JSON out, UTF-8, no versioning in the hostname.

**Every list response is an envelope**, never a bare array:

```json
{ "items": [], "count": 0, "next_cursor": null }
```

`count` is the number of items in *this* page. A bare array cannot grow a cursor or a total without
breaking every client, and lists that never paginate are a temporary condition.

**Every detail response is the object itself**, unwrapped. A singular envelope buys nothing.

**Errors** are uniform:

```json
{ "code": "not_found", "message": "human readable, never parsed", "detail": {} }
```

`code` is the contract; `message` is for people. HTTP status carries the same information for
anything that only speaks status codes, but clients should branch on `code`.

**Unknown query parameters are rejected**, `400` with `code: "unknown_parameter"`, and `detail`
carrying `unknown` (the offending names) and `accepted` (everything that route declares). The
default web-framework behaviour — ignore what you do not recognise — is the wrong one for a
filtered collection: a misspelt, retired, or invented filter answers `200 OK` with the whole set,
and the response is indistinguishable from a correctly filtered one. A silently ignored filter is
a lie the caller cannot detect, and both clients shipped months of it. The accepted set is derived
from the matched handler's own signature at request time, under each parameter's wire name, so
aliases (`?class=`), repeated params (`?label=k=v&label=...`), and new filters need no registration
anywhere. `/health` and the OpenAPI routes are exempt.

## 2. Identity and scope

- `Authorization: Bearer <token>` — required. Two kinds of token, one header.
- `X-Hangar-Project: prj_...` — scopes the request to a project. Not yet enforced server-side;
  reserved.

Two principal kinds share the header:

- **operator** — cluster agents. The token is a static, per-deployment service credential,
  compared to the configured value in constant time. Never rotated per-request, never derived from
  anything sent over this API.
- **admin** — humans, via `admin` and `hgr`. `POST /v1/auth/login` takes `{"password": "..."}` and
  returns `{"access_token": "...", "refresh_token": "...", "expires_in": <seconds>}`. The access
  token, not the password, is the bearer credential on every request after; it is short-lived and
  signed, and it is prefixed `hgrs1` where a refresh token is prefixed `hgrr1`, so the two cannot be
  confused. `POST /v1/auth/refresh` takes `{"refresh_token": "..."}` and answers with the same three
  fields, **rotating the refresh token as it does so**: the one it was given stops working, so a
  stolen refresh token and the real one cannot both stay valid. `POST /v1/auth/logout` takes the
  same body, revokes it, and answers `204`. Sending the password itself as a per-request credential
  is deliberately not supported: it would end up in proxy logs and could not be rotated
  independently of the person who knows it.

  **Login is budgeted.** Ten failed logins in a rolling minute, per process, and the route answers
  `429 too_many_requests` without verifying a password — the check runs before bcrypt, because the
  thing being bounded is the CPU an anonymous caller can buy and not only the guesses. A correct
  password is refused for the rest of that window too, which is the accepted cost.

A request with no token, an unrecognized token, or an expired or tampered session token gets
`401 not_authorized` in the standard error envelope. `/health` is exempt so probes do not need
credentials.

A cluster's **enrolment token** is a third kind of credential and deliberately not a third principal
kind. It authenticates that cluster's operator to the hub ([`PROTOCOL.md`](PROTOCOL.md)) and its
serving agent to the gateway, and this API never accepts one in `Authorization`. The one route
that takes it takes it in a request body, from an operator principal, and answers with a cluster
identity rather than with a session (§6). Two principal kinds is a number small enough to review in
one function; a third exists the moment something needs a permission model, and nothing does yet.

**`ADMIN_AUTH=disabled`.** A day-1 deployment with no ingress — reached only by
`kubectl port-forward` — can turn off the admin half of this check entirely; every request then
resolves to a full-scope admin principal with no credential required. The hub's operator-token
check has no equivalent switch and is never affected by this setting: it authorizes writing
observed state for the whole fleet over a socket agents dial in on the network, which a
port-forward does not protect. Turning `ADMIN_AUTH` off is an explicit operator choice, never a
side effect of leaving credentials unset, and the plane logs a warning naming the exposure on every
startup while it is active. Flip it back to `enabled` (or unset it outside local development, where
it is the default) before this plane is ever reachable through anything but a port-forward.

Every handler resolves a caller identity through one function (`resolve_principal`), so a future
OIDC/SSO integration replaces how a `Principal` is produced without touching a single route.

## 3. Identifiers

Prefixed, opaque, stable: `clu_ nod_ dev_ tpd_ pol_ art_ rec_ dep_ rep_ end_ prj_ qta_ opr_ cmp_
ten_ prc_ plc_ alc_ wkl_ evt_`.

The body is a Crockford base32 ULID, **lowercase**. Crockford is case-insensitive when decoding, but
these ids are opaque strings that get compared, indexed and put in URLs, so exactly one spelling is
canonical — two spellings of one id is the same problem as two ids.

A path segment accepts **either the id or the name** where an object has a unique name
(`GET /v1/clusters/iad-1` and `GET /v1/clusters/clu_01hq0v8p2m` are the same request). Prefix
matching is **not** supported by the API — `hgr` resolves prefixes client-side, and that is where
it belongs, because a server that guesses at ambiguous input is a server that eventually guesses
wrong.

Timestamps are RFC 3339, UTC, millisecond precision. Nullable where the event may not have
happened; `last_sync_at` on a never-synced cluster is `null`, not the epoch.

## 4. Declared and observed in one payload

An object's declared and observed halves are returned together but **never merged**. The rule is
visible in the field names:

```json
{
  "id": "clu_01hq0v8p2m",
  "name": "iad-1",
  "phase": "active",
  "reachable": true,
  "sync": { "state": "connected", "last_sync_at": "...", "lag_ms": 340 }
}
```

`phase` is declared — what we asked for, one of `pending | active | draining | disabled`.
`reachable` and `sync` are observed — what is true right now.

**This is the resolution of the biggest conflict between the two clients**, and it is not
cosmetic. Folding reachability into phase as an `unreachable` value makes "the network is down"
indistinguishable from "someone disabled this cluster." Those need different responses from a human
and different behaviour from the placement layer, so they cannot share a field.

The same rule applies wherever both halves exist: a Component carries `declared_version` and
`observed_version` as a pair, not one nullable column. A Node carries `cordoned` as an observed
boolean and not as a value in a condition enum, because draining is an Operation and cordon is the
state it leaves behind.

## 5. What the API does not expose

- **No `api_endpoint` on a cluster.** The plane never needs the address — the operator dials out
  and identity is its credential, so an address would be decoration that looks like configuration.
  The one address a cluster carries is `serving` (`PUT /clusters/{ref}/serving`): where the *data*
  plane reaches its serving runtime, and a credential reference for whatever fronts it. The plane
  stores and hands it out; only the gateway ever dials it.
- **No Kubernetes-specific fields.** No `kubelet_version`, no manifest fragments, no object
  references. Generic observed attributes (os image, kernel version, driver version, operator version)
  are fine. The operator is the only thing that knows Kubernetes exists; leaking it here would make
  the plane's API a Kubernetes API with extra steps.
- **No telemetry on an object.** Utilization, temperature, throughput, latency, token counts are
  never fields on a device, a node or a deployment. Health, allocation, membership and residency are
  state and belong here; measurement is written to the observability stack directly and read back
  from it under `/v1/metrics`, which belongs to no object and is a read-through rather than storage
  — see §6.
- **No failure-rate metric from an engine exporter, and no availability derived from one.** A
  request that never reached a cluster — refused at the front door, routed nowhere, timed out before
  an engine saw it — leaves no trace in what that engine reports about itself, and those are the
  requests an availability incident is made of. So the vocabulary has no `inference.failure_*`:
  `serving.failure_ratio` and `serving.availability` are gateway measurements or they are absent.
  Where the threshold that calls a number an outage lives is a separate matter — not here, because a
  threshold is not a measurement.

## 6. Resources

Declared objects take the full set: `GET` list, `GET` one, `POST`, `PATCH`, `DELETE`.
Observed objects are **read-only** — there is no `POST /v1/devices`, because devices are discovered,
not created. Writing to an observed resource is a category error and returns `409 invalid`.

| Path | Kind |
|---|---|
| `/v1/clusters` | declared registry, observed body, plus an agent-token check — see below |
| `/v1/nodes`, `/v1/devices`, `/v1/topology-domains` | observed, read-only |
| `/v1/device-classes` | the shared dictionary, read-only — see below |
| `/v1/pools` | declared, with observed membership |
| `/v1/artifacts` | declared, plus two read-only derived views, one operation, and a declared baseline sub-resource — see below |
| `/v1/workloads`, `/v1/endpoints` | declared, plus a read-only resolution — see below |
| `/v1/deployments` | declared, per-cluster, read-mostly — see below |
| `/v1/discovered` | observed, read-only, plus the decision verbs — see below |
| `/v1/replicas`, `/v1/allocations` | observed, read-only |
| `/v1/tenants`, `/v1/projects`, `/v1/quotas`, `/v1/priority-classes`, `/v1/policies` | declared |
| `/v1/components` | declared version, observed rollout |
| `/v1/placement` | one dry-run verb, writes nothing — see below |
| `/v1/operations` | see §7 |
| `/v1/benchmarks` | declared run, observed result — see §7.1 |
| `/v1/events` | observed, read-only, append-only |
| `/v1/metrics` | measurement, read-only, belongs to no object — see below |
| `/v1/alerts` | derived, read-only apart from two lifecycle verbs — see below |
| `/v1/notification-channels` | declared, plus a `test` verb |
| `/v1/system/config` | the process's own configuration, read-only — see below |

**`GET /v1/system/config` is the plane telling you what it is running with**, and it is not an
object in the model — it is the process, so it lives beside `/health` and `POST /v1/auth/login`
rather than under a resource. It answers the question a misconfigured metric store raises: which
cluster is mapped to which store, under which `cluster` label, and is there a shared endpoint at
all. Alongside that it carries the resolved app environment, log level and format, the hub's
host/port/path and public URL, the reactor's switch and intervals, the session TTL and reaper
window, the database's driver/host/port/name and pool sizing, and the operator image and chart
ref/version the register-cluster screen hands out.

**The exposed set is an allowlist, and a denylist here would be a defect.** Every field is named
by hand in the response schema, so a credential added to the plane's settings is absent from this
endpoint until somebody decides otherwise; a rule that skipped suspicious names would publish the
next one by default. Three consequences are binding on any implementation: a credential is
reported only as a boolean — never a value, a prefix, a length or a digest; `DATABASE_URL` is
reported in parts and never as a string, so userinfo cannot travel; and the per-source `|token=`
never appears, because a metrics source is public as endpoint, kind and label only. Admin
principals only — an operator token is `403 forbidden`, since a cluster agent has no use for the
plane's process configuration.

**An endpoint's `slug` is the model id clients send; its `name` is an internal handle.**
`name` is in URLs, in metrics labels and in every link the console holds, and it is not patchable —
an endpoint exists so that deployments can be replaced without anybody noticing, and renaming it
spends exactly that. `model` in an OpenAI request is a public contract and wants neither of those
properties: it wants to read `moonshotai/kimi-k3` rather than whatever the endpoint was called when
somebody made it, and it has to be correctable afterwards. So they are two fields, and this is the
one that can be patched.

`slug` is never empty. Left out at creation it takes the slug of the weights behind the first
target, which is the case that should be normal — an endpoint in front of one model answers to that
model, and requiring a caller to type the name a second time is how the two come to differ.
Declaring one is then unambiguously a request for an alias.

There were four fields here — `name`, `model_slug`, `display_name`, and a `resolved_model_slug`
computed on the way out purely to report which of the first two had won. That is what a nullable
public identifier costs, and none of them on its own answered "what does a client send".

The resolved table reports the slug as the endpoint's `name`, which is the key the gateway builds
its route map on — so changing a public model id is a plane-side write and one gateway poll, with no
engine flag, no pod respec and nothing on any cluster.

**`GET /v1/endpoints/resolved` is derived and belongs to no object.** An endpoint declares a name
and a set of targets; the resolution is what that means right now — each enabled target followed
to its deployment, that deployment's cluster, and the serving address the cluster carries. It is
computed on read and stored nowhere, because a materialised copy is a second version of the same
fact with its own way of being stale. The whole table comes back unpaginated: a caller that
assembled half a routing table would route half its traffic into a hole. It is `ETag`ged by a
revision covering every table it was built from, so polling it costs a `304` until one of them
moves. An endpoint with no usable targets is returned with an empty upstream list rather than
omitted — "no such model" and "this model has nowhere to go" are different answers.

**Each upstream carries `served_model_name`, and it is usually not the endpoint's name.** A client
addresses an endpoint by its public name; the engine behind it answers to the model id the plane
resolved for it — by default the artifact's lowercase Hugging Face slug, `moonshotai/kimi-k3`. The
gateway rewrites the request's `model` to this before forwarding, so the public name never has to
equal the served one. It rides the upstream rather than the endpoint because one endpoint can front
two clusters running different serving configurations, and there is no single answer to put one
level up.

**It is `Artifact.slug`, stored, and nothing else declares it.** The slug is written once on the
way in, derived from the location that names the weights: `hf://moonshotai/Kimi-K3` becomes
`moonshotai/kimi-k3`. Lowercase because that is what a caller types; the organisation kept because
a bare `kimi-k3` is not a model id anybody can look up.

There used to be a ladder — the workload's `served_model_name`, then the recipe's name, then a slug
the plane re-derived at read time from the artifact's `name`. Every rung was wrong in its own way.
The recipe's name described the fit, `llama-3-3-70b-instruct-fp8-tp2-32k`, and moved whenever the
fit was redone. The read-time derivation could not produce an organisation at all, because a name
is `gpt-oss-20b` and the `openai/` only ever existed in the location URI that nothing consulted —
while the operator started every engine on a `--model` taken from that same location. So the plane
published one string and the frontend registered another, and the failure is invisible from both
ends: the deployment is healthy, the worker is ready, the tunnel is open, and the request comes back
`model_not_found` from inside the cluster. Storing it once removes the disagreement rather than
adjudicating it.

The workload's `serving.engine_args` under the flag's own spelling, `served-model-name`, still beats
it and remains the last-resort hatch — those keys are rendered onto the engine command line
verbatim, so the hyphenated form is the only one that both reaches the engine and is read here. An
escape hatch that loses to a default is not one.

**`served_model_name` on the endpoint target beats it, and it is now only for adopted deployments.**
It exists for a process the plane did not render — somebody else's engine, answering to a name we
can observe and not choose. It was load-bearing while the plane derived the engine's name a second
time from its own rule, because two derivations of one value drift and these did. With the slug
stored on the artifact and rendered from the same column, the ordinary case has nothing to correct.
Null, the ordinary case, means the artifact's slug stands. Setting it costs a gateway poll;
correcting the same mismatch on the workload's serving block respecs the pod and reloads the
weights, which for a checkpoint the size of Kimi K3 is twenty-two minutes of downtime to change a
string nobody outside the cluster can see.

A cluster contributes upstreams only while its `phase` is `active`. `draining` and `disabled` are
somebody declaring that the cluster must stop taking work, and traffic is the first thing that has
to obey it — a target on such a cluster disappears from the resolution exactly as a disabled target
does, and the endpoint comes back with fewer upstreams or with none. The cluster's phase is one of
the facts the revision covers, so marking one `draining` invalidates the `ETag` and a poller stops
holding the route behind a `304`.

**`POST /v1/clusters/tunnel-auth` is the gateway asking who just dialled it**, and it is the only
route in this API that carries a credential in a body. A serving agent inside a GPU cluster opens
an outbound tunnel to the gateway — nothing dials into a cluster — and identifies itself with the
cluster's enrolment token, the same string the operator authenticates to the hub with and delivered
by the same chart into the same Secret. The gateway cannot check it alone: only the plane holds the
hash, and shipping every cluster's token material to the machine that faces the internet is exactly
the trade a control plane exists to avoid. So the gateway posts `{"token": "hgro1..."}` and gets back
`{"cluster_id": "clu_...", "cluster_name": "iad-1"}`, or a refusal. Nothing is minted and nothing is
stored; no hash, prefix or length of the token appears in any response.

The caller's own credential stays in `Authorization`, which is why the token being verified travels
in the body — one request, two credentials, and only one of them belongs to the caller. **Operator
principals only**: an admin session is `403 forbidden`, because a human credential that can turn
tokens into identities is one that can sweep for them, and no screen asks this question. Every
refusal is `401 not_authorized` so a caller branches on one code, while the message says which
refusal it was, since a person reading an agent's logs needs to know whether the token is wrong or
the cluster is out of service.

Only `disabled` is refused. A `draining` cluster's tunnel still has to carry the streams already on
it — its upstreams are gone from the resolution above, which is how new traffic stops — and a
`pending` cluster may bring its agent up before its operator has completed a handshake. Refusing
either would sever in-flight work or make tunnel setup depend on the order two Deployments happen to
roll. Failed verifications are budgeted per API replica, twenty in a rolling minute, then
`429 rate_limited` until the window drains; a *known* token belonging to a disabled cluster spends
nothing, or one misconfigured cluster's retry loop would deny every other cluster's dials. The budget
is not sized against guessing — the token is 256 bits of random and the route needs the gateway's own
credential to reach at all — it is there so a leaked gateway credential or a looping agent costs
the plane a bounded amount of work and announces itself.

**This is a connection-setup call, not a per-request one.** The gateway verifies once per tunnel and
holds the identity for the life of that connection, so a plane that is down, redeploying or migrating
its database does not break inference: established tunnels keep carrying traffic and only an agent
dialling in — a first start, a restart, a reconnect — waits for the plane to answer again. That is the
same property `GET /v1/endpoints/resolved` has, where the gateway keeps serving the last table it
read, and it is deliberate in both places, because the alternative is that this project's uptime
becomes the inference SLA. The plane also learns nothing from the call: whether the tunnel then
works, how long it lasts and when it goes away are gateway measurements, and there is no route here
that writes them back — a tunnel is not an object in the model and reachability has one writer.

Sub-resources are addressed by filter, not by nesting: `GET /v1/devices?cluster=iad-1`, not
`/v1/clusters/iad-1/devices`. One collection per object type keeps filtering, pagination and
permissions in one place instead of N.

**`/v1/device-classes` is a dictionary, which is a fourth kind alongside declared, observed and
derived.** A device class is the identity a GPU model is placed against — `h100-80gb-sxm` on a
pool, a workload's serving shape, a quota and a device — and it is defined in `devices.json` in this package,
read by the plane and embedded in the operator. Nothing declares one and no cluster reports one, so
there is no `POST` and no `PATCH`: adding hardware is a change to the contract table and a release
of both halves. Editing it over the API would leave the plane and the operator resolving vendor
strings against different dictionaries, which is the one thing a shared table exists to prevent.

Each entry carries `name` — the identifier, and the only string that travels on the wire — and
`label`, the only string to put in front of a person. Alongside them is `inventory`: how many
devices of that class the fleet holds, how many are healthy, how many are allocatable — free, so
claimable and unclaimed — how many are allocated, and which clusters they are in. Counted from
observed state on read, never stored. `allocatable` and `allocated` are separate counts and neither
is the complement of the other: a device that is unschedulable and unheld is in neither, and a held
device may be unhealthy.

The collection is the union of two sets, and a client must expect both. A class in the table with
no devices is hardware we could take delivery of tomorrow. A class in the fleet with no table entry
is hardware nobody has named yet — it resolved to the slug of its vendor string, it is being
scheduled onto, and it comes back with `in_catalog` false and no vendor metadata. The second is the
one to act on. Filters: `cluster`, `architecture`, `present`. Not paginated — the dictionary is
bounded by the hardware that exists, not by use.

**`/v1/alerts` is derived, which is a third kind alongside declared and observed.** Nothing on a
cluster reports an alert and nothing declares one: the reactor computes them by reading observed
state back against declared state, and by matching `/v1/events`. So there is no `POST /v1/alerts`
and no `DELETE` — resolving is how an alert ends, and deleting one would erase the record that it
happened. Two verbs write the one part a machine cannot know: `POST /v1/alerts/{ref}/acknowledge`
(optional `note`; the acknowledger is the request principal and is never taken from the body) and
`POST /v1/alerts/{ref}/resolve`.

An alert carries two independent axes. `severity` is how bad it is; `state` is where it is in its
life — `pending` (true, but not for long enough to be worth telling anyone), `firing`,
`acknowledged`, `resolved`. Conditions are level-triggered and resolve themselves when they stop
being true; alerts derived from an event never do, because an event log does not retract. Filters:
`state`, `severity`, `rule`, `cluster`, `subject`, `since`.

**Alerting is tuned with labels, not with a field of its own.** `labels` is already an editable
dict on a workload, a cluster and a discovered workload, so the reactor reads a reserved namespace
out of it rather than adding a column that would need its own migration, form field and CLI flag —
and that would only work for the one object it was added to:

| Label | Values | Effect |
|---|---|---|
| `hangar.io/alert` | `off` / `on` | `off` mutes every condition about this object |
| `hangar.io/alert.severity` | `info` `warning` `error` `critical` | overrides the rule's severity |
| `hangar.io/alert.for` | `45m` `2h` `900s` `900` | overrides how long it must hold before firing |

They are set on the **workload**, which is the only object that carries them: a workload's serving
configuration is its own and is not shared with anything, so muting one workload of a model never
quietens another. There is no per-cluster override either; muting a workload mutes it in every
cluster it runs in, which is what a workload-level knob should mean. Cluster conditions read the cluster's labels,
and a workload nobody has imported yet reads the labels the cluster reports for it — which makes the
manifest the only place that knob exists before promotion. An unparseable value is ignored rather
than obeyed, and a tuned alert records what changed it under `details.tuned_by`. Muting an alert
that is already firing resolves it on the next tick, because absence from the evaluated set is what
closes a condition.

**`/v1/notification-channels` never returns a webhook URL.** A Slack incoming webhook is a bearer
credential in URL form, so `webhook_url` is write-only and reads back as `webhook_hint` — the host
and the last few characters, enough to tell two apart and not enough to use. `POST
/v1/notification-channels/{ref}/test` posts a sample message and answers `200` with
`{"delivered": false, "detail": "..."}` when the webhook rejects it: the failure is Slack's, not the
plane's, and the reason is the useful part.

**`/v1/metrics` is measurement read back, and it is one answer shape for every scope.** There is no
`POST` anywhere under it and nothing to write: the plane forwards a question to whichever store
holds the series and reduces the result. It is not a field on a device or a deployment because the
two have different failure modes and different freshness — an inventory read must never get slower
because a Prometheus is unreachable — and it is not the data plane either: the numbers were written
to the store directly and this is only a reader.

Every route answers the same object: `scope` (which question was asked), `ref` (which object, or the
scope's own word where there is none), `cluster` (null when the answer belongs to no single cluster),
`source` (the store that answered, as `cluster`, `url` and `kind` — never its credential), `window`
(the lookback actually used), `samples[]` of `{metric, title, unit, value}`, `breakdown[]` of
`{key, label, samples[]}`, `series[]` of `{metric, title, unit, labels, points[]}`, and
`unavailable`. **`value: null` means the
store had no series, which is not zero** — an idle GPU reads 0 and an unmeasured one reads null, and
rendering the second as the first is how a panel comes to claim a fault that is not there. Same for
`unavailable`: it is the reason there is nothing, in words, and it is meant to be shown rather than
replaced with a number.

`GET /v1/metrics/sources` is the exception and answers its own shape: which clusters can be measured
at all, which is the first thing to check when a panel is empty. Everything else is one of these:

| Route | Scope | `ref` | `cluster` |
|---|---|---|---|
| `GET /v1/metrics/clusters/{ref}` | `cluster` | cluster uid | the cluster |
| `GET /v1/metrics/clusters/{ref}/device-classes/{name}` | `device_class` | the class name | the cluster |
| `GET /v1/metrics/pools/{ref}` | `pool` | pool uid | the cluster |
| `GET /v1/metrics/nodes/{ref}` | `node` | node uid | the cluster |
| `GET /v1/metrics/devices/{ref}` | `device` | device uid | the cluster |
| `GET /v1/metrics/deployments/{ref}` | `deployment` | deployment uid | the cluster |
| `GET /v1/metrics/workloads/{ref}` | `workload` | workload uid | **null** — a workload spans clusters |
| `GET /v1/metrics/fleet` | `fleet` | `"fleet"` | **null** — every cluster at once |
| `GET /v1/metrics/serving` | `serving` | `"serving"` | **null** — measured at the gateway |
| `GET /v1/metrics/endpoints/{ref}` | `endpoint` | endpoint uid | **null** — an endpoint reaches no cluster |

Every one of them takes `window`, `step`, `span` (seconds, default `3600`) and `series` (default
`false`), and nothing else — `series=false` is the normal read and the points are only computed when
asked for. `GET /v1/metrics/workloads/{ref}` also takes `project`, because a bare workload name is
unique only within its project; an ambiguous one answers `409 invalid` listing the candidates, which
is exactly what `GET /v1/workloads/{ref}` does with the same reference. None of these is a list, so
none of them accepts `limit` or `cursor`, and passing one is `400 unknown_parameter` like anywhere
else.

**Metric names are Hangar's vocabulary, not PromQL, and the family says who emitted the number.**
`gpu.*` is what the device exporter reports, `inference.*` is what a serving engine says about
itself, and `serving.*` is what the gateway saw. The percentiles in the vocabulary are named because
somebody is held to them rather than because a caller can pick a quantile — `inference.ttft` (p95)
beside `ttft_p50`, `ttft_p90` and `ttft_p99`, and `tpot_p99` and `latency_p99` beside their p95s.
Each is quantiled out of the engine's own buckets in its own right: a tail inferred from the
percentile next to it would be a number nothing measured, which is worse than a cell that says it
was not measured. `serving.latency_*` is not `inference.latency_*` and the two disagreeing is not a
defect — the gateway's includes the hop, the queue in front of it and the requests that failed
before an engine saw them.

**A workload is measured once per cluster it landed in and merged afterwards, and a percentile is
not merged.** The GPU family is selected by the devices its live allocations hold — absent, not
zero, in a cluster where it holds none — and the inference family by the same worker-then-model rule
`deployment` follows. `breakdown[]` carries one row per cluster, keyed by cluster uid, each with that
cluster's own numbers. In the merged `samples[]` sums add, temperatures take the worst card and
utilizations average, but a metric read out of a histogram quantile is **omitted** when more than one
cluster answered it: two p95s have no mean, so the merged figure is absent and the real per-cluster
ones stay in the breakdown.

**`GET /v1/metrics/fleet` is one widened query or a refusal.** The clusters are widened into a single
`cluster=~"…"` matcher that one store aggregates over itself, rather than fanned out and added up
here — the plane is not the aggregation layer, and the arithmetic is the reason. So it answers
`samples: []`, `breakdown: []` and an `unavailable` string when that cannot be done honestly: when
the fleet's clusters sit in more than one store ("ask each cluster"), when a store answering for a
cluster carries no cluster label to narrow by, and when no cluster has a store at all. It carries a
deliberately short vocabulary — traffic, its tail, and throughput — because a fleet answer is one
query per metric over every cluster's series and the whole vocabulary would be a page of them.

**`GET /v1/metrics/serving` and `/v1/metrics/endpoints/{ref}` are read from the gateway and from
nowhere else**, which is the whole point of the `serving` family (§5): the requests that make an
availability number interesting are the ones no cluster ever saw. The join is exact — the endpoint
uid is a label on the gateway's series — and the total is asked for as a total rather than assembled
from a page of endpoints, which would move when somebody pages. `breakdown[]` on `/serving` is one
row per endpoint, and the gateway's two reserved keys are labelled rather than hidden: `_catalog` is
`GET /v1/models`, answered from the routing table by no endpoint at all, and `_unknown` is a caller
naming a model we do not serve — the row worth watching when a rename has gone wrong. An unknown
endpoint reference is `404`. Until something scrapes the gateway these answer with every `value`
null, or with `unavailable` set if no store is configured for them at all: an endpoint nobody has
called is not 100% available, and it is not 0% either.

**`/v1/workloads` is what you create; `/v1/deployments` is what that became.** A Workload is the
global declaration — a serving configuration, a total scale, a kind, a priority, and the regions or
clusters it may use. A Deployment is that workload on **one** cluster, and it is still exactly what the operator
reconciles. "Run gpt-20b on asia and europe" is one workload, two deployments, one replica each.
Nothing about a Deployment's meaning changed; a layer was added above it.

The split follows the fact, not the object: what is true of the whole workload (the serving
configuration, adapters, overrides, labels, kind, priority class, preemptible, gang size, deadline,
the topology-domain pair, and the min/max/desired totals) lives on the Workload, and what is true of
one cluster (admission phase and reason, the queued/admitted/placed timestamps, generation, enabled,
authority, adoption) lives on the Deployment.

**How a workload is served is a block on the workload, not an object beside it.** There is no
`/v1/recipes`: the engine, its images, the parallelism, the cache settings and the resource request
are a nested `serving` object on the workload, sent on `POST /v1/workloads` and returned by
`GET /v1/workloads` and `GET /v1/workloads/{ref}`. It mirrors `deployment.desired.serving` in
`MESSAGES.md` field for field — `engine`, `engine_runtime_version`, `engine_runtime_image`, `frontend_image`,
`engine_args`, `quantization`, `dtype`, `parallelism`, `context_length`, `max_batch_tokens`,
`kv_cache_dtype`, `kv_cache_fraction`, `prefix_cache_enabled`, `prefix_cache_config`, `resources`
and `overlay` — and adds `artifact`, which is the weights this configuration serves and is required,
plus `capabilities` and `imported_args`. `served_model_name` stays on the workload beside the block
rather than inside it, because it is a public name and not part of the footprint.

Reuse is not something a caller arranges. Two workloads declaring the same configuration are not
made to share a row; they simply have the same digest, and that is the whole mechanism.

**`serving_digest` is derived, read-only, and is the identity of the configuration.** `svg_` followed
by a sha256 over the canonical JSON of exactly the fields that reach an engine — the block above,
with the artifact as its uid. The plane recomputes it on every write and a body carrying one is
`409 invalid`: it is an answer, not an assertion. `imported_args`, `capabilities`,
`served_model_name` and `labels` are deliberately outside it, as are scale and placement, because the
question it answers is "would this produce the same running process" and none of those change the
process.

**`capabilities` is asserted and `imported_args` is observed, and neither is ever rendered.**
`capabilities` is what somebody says this configuration can do — tools, vision, structured output —
and it reaches no cluster and no engine flag. `imported_args` is the argv a discovered workload was
actually running, recorded in full so that the person confirming an import can check it against the
cluster; it is never turned into flags, because reading a flag off a running pod records it and
asserting it is a separate act. Only `engine_args` is rendered.

**`resolved_engine_args` is derived on read and stored nowhere.** It is the flags the operator would
render: the ones the plane derives from the block — parallelism, context length, dtype, quantization,
the KV-cache settings — with `engine_args` laid over the top, so a key the caller declared wins over
the derived one under the same spelling. It is on the read shape only, it is never accepted on a
write, and it is the one place to look before asking why an engine came up with a flag nobody typed.

**Scale is declared as a total and read as a share.** `min`, `max` and `desired` on a workload count
across every cluster it may use; each deployment's `min_replicas` / `max_replicas` /
`desired_replicas` are **that cluster's share of the total, never the total**. The plane divides;
nothing else sums, scales, or re-derives. Summing a share against a total, or dividing one out of
the other, is always a bug. The only legal summing is on the workload's own `scale.placed` and
`scale.ready`, which the plane computes from its deployments.

The unit of those three numbers is named by `kind`, because they count different things: `replicas`
for `service`, `ranks` for `gang`, `completions` for `task`. A bare integer without the unit is
unreadable, so `scale.unit` is on the read shape.

**Kind also decides whether a workload may be split.** `service` and `task` are splittable —
`8 = 5 + 3` is fine, and for a task splitting is the point. **`gang` is atomic**: its ranks must be
co-scheduled, so a gang workload has exactly one Deployment, always. A gang that cannot fit in one
cluster is a hard rejection with a per-cluster verdict, not a lower-ranked spread; a split gang is
broken rather than degraded. The schema permits many deployments and the kind constrains gang to
one.

`strategy` is `pack` (default) or `spread`. `pack` fills one cluster before opening another, which
is why every existing single-cluster deployment is unchanged by this layering and why fanning out is
opt-in. `cluster_floors` sets a per-cluster minimum share, `max_clusters` caps the fan-out, and a
named `cluster` pins the whole workload to one — a deliberate escape from the scheduler, checked for
existence and not for capacity.

**Where a workload may run is a Policy, not a field.** `allowed_regions`, `allowed_residencies`,
`allowed_device_classes`, `allowed_clusters` and `denied_clusters` already exist on
`/v1/policies`, which now accepts `workload` as a subject. A second copy of those five lists on the
workload would be a second answer to a question that already has one.

**Nothing below a Workload branches on `kind`.** The per-cluster noun is singular across all three
kinds — a task workload gets Deployments too, and there is no Job or Run type — because the
supertype exists so that preemption and quota stay one code path instead of three. The only
component entitled to read `kind` is the operator, deciding what to render.

**`/v1/deployments` is read-mostly.** `POST /v1/deployments` and `DELETE /v1/deployments/{ref}` do
not exist: placement creates a deployment when it expands a workload and removes it when it stops
placing there. Taking a workload off one cluster is a `workload.drain` operation naming that
cluster, or a scale change — not a DELETE of the per-cluster row. What remains is `GET` list and
detail, a `PATCH` narrow enough to hold only per-cluster switches, and the authority verb below.
Filters: `cluster`, `workload`, `project`, `phase` (which reads the deployment's admission phase).

**`/v1/discovered` is the one observed collection that accepts a `POST`**, and the exception proves
the rule: `POST /v1/discovered/{ref}/track` and `.../ignore` do not write the sighting, which stays
ingestion's alone. They write a decision beside it — one row saying we looked at this and want it in
the inventory, or do not. Neither produces a `deployment.desired`, and that is the guarantee the two
verbs exist to make: declaring a workload that is already running is how a control plane restarts
it, and reloading a model into VRAM costs minutes of served traffic. Adoption is a decision to
observe, never a decision to declare. `?status=new|tracked|ignored|dismissed|promoted` filters on
the decision.

**`DELETE /v1/discovered/{ref}` deletes nothing**, and is a DELETE anyway. What a caller removes is
the entry in front of them; the sighting under it is observed, ingestion is its only writer, and a
row deleted here would be written straight back within an observation interval because the workload
is still running. So the removal is recorded as a decision and the listing honours it — which is
also what makes it undoable, where a delete racing an observation would not be. `POST
/v1/discovered/{ref}/restore` drops the decision and the entry returns; `?status=dismissed` lists
what has been removed. A promoted sighting cannot be removed: a deployment names it.

**`POST /v1/discovered/{ref}/promote`** is the one that does declare, and it returns a Workload
because that is what it made — a workload pinned to the cluster the sighting was seen on, with the
single Deployment under it that the sighting became. The caller sends back the `spec_fingerprint` they were shown and the
promotion is refused if the workload changed since; the digest is then recorded on the deployment as
`adopted_fingerprint`, and the cluster refuses to reconcile it while the live object digests to
anything else. Scale is not a parameter — the bounds are taken from what the workload is already
running, because a promotion that also set the replica count would scale a live workload as a side
effect of cataloguing it. From there the object is managed through `/v1/workloads` like any other, and its one per-cluster
row through `/v1/deployments`.

A promoted deployment is created `observed`: recorded and verified, with the plane writing nothing
to the cluster.

**`POST /v1/deployments/{ref}/authority`** moves it between `observed` and `managed`, and is the only
thing that decides whether the operator may act (ADR 0009). The two directions are not symmetric.
Taking management of an adopted workload requires the `fingerprint` the caller was shown, compared
against the *current* sighting rather than against `adopted_fingerprint` — that field records what
was true at promotion, and the question here is whether you are looking at what is running now.
Releasing back to `observed` needs nothing and is always allowed: the plane letting go never writes
to a cluster, so there is no gap to fail closed on. A deployment the plane created is `managed` from
birth, having never been anything else.

**`/v1/tenants` exists because a project cannot be created without one.** This document used to say
there was no tenant collection and that a project carries its tenant as an embedded field. The
second half still holds — a project always carries the tenant it hangs off, so no reader has to
join — but the first half made an empty plane unusable: `POST /v1/projects` resolves `tenant` to an
existing row, nothing in the API could create that row, and so the first project could never be
made, and with it no quota, no deployment and no promotion. The collection is deliberately thin: a
slug, a name and a switch. `slug` is absent from `PATCH` because it namespaces every project slug
beneath it, and `DELETE` is refused with `409` while any project still hangs off the tenant — the
foreign key cascades, and a delete that silently took quotas and allocations with it is not a delete
anyone asked for. `project_count` is on the read shape so a caller can see what a delete would
refuse over.

**`GET /v1/discovered/{ref}/serving-draft` reads a sighting as the objects needed to adopt it.** A
`GET` because it is one: the same sighting derives the same draft every time, and asking changes
nothing. It returns the proposed `serving` block — engine and version from the image tag, context
length and parallelism from the container's argv — and, under `artifact`, the weights that block
would point at. The draft's `imported_args` is the whole argv as flag/value pairs, including
arguments that also populated a first-class field, because a record that keeps only what it has
columns for cannot be checked against the cluster by the person confirming it. Its `engine_args` is
always empty: reading a flag off a running pod records it, and asserting it is a separate act by a
person. The artifact half exists because a serving configuration cannot be written without one and a
workload the plane never declared has no artifact anywhere: `--model org/model` becomes an `hf://` source and a
`digest` of `hf:org/model`, with the revision appended when `--revision` was given. That digest is a
name, not a hash, so the artifact is written with `verified: false` and stays that way until
something resolves the bytes. `artifact.existing_id` is filled when the catalogue already holds
those weights, which makes adopting one a confirmation instead of a duplicate. Nothing on this path
writes; `POST /v1/artifacts` and `POST /v1/workloads` do, on their own modules' endpoints, so there
is one way to create each object rather than two that must agree.

**`GET /v1/artifacts/resolve?source=hf://org/model` answers what an artifact would be.** A `GET`
for the same reason `serving-draft` is one: the same source resolves to the same answer every time,
and asking changes nothing — no row is written, no id is minted, and calling it twice costs nothing
but a Hub round trip. It returns the draft the create would have built: the derived digest, the
name, format, size, licence and parameter count, the source locations, a `serving` block of
suggested device count and context length that is a suggestion and is never applied by anything, and
`existing_id` when the catalogue already holds that digest. It is a preview endpoint and not a
second door into the catalogue: `POST /v1/artifacts` remains the only way to create one, so the
claim above — one way to create each object rather than two that must agree — still holds. Unknown
slug is `404 not found`; a gated repository is `409 invalid` naming the gate and the token variable
to set, because the fix is a credential and not a retry; a malformed source is `409 invalid`; the
Hub being down or refusing is `500`, since the failure is upstream of us and the caller's request
was fine.

The `serving` block is derived in one direction only: **the serving parameters come from the
repository, and the device shape is derived from them.** `context_length` is the window the config
declares, `quantization` and `dtype` are what the repository ships, and `device_count` and
`device_memory_mib_min` are the hardware that configuration would need. An artifact knows nothing
about what we operate, so it never trims a 131k window down to what a card happens to hold — sizing
the weights to the hardware is the workload's decision, made against real device classes, and this
endpoint has no business pre-empting it. What comes back seeds a workload's `serving` block; nothing
is applied until somebody creates the workload.

`notes` is a list of `{level, message}`, `level` being `info` or `warn`. Two different things get
said here and a flat list of strings makes them look alike: that a repository ships a duplicate copy
of its weights under `original/` is a packaging fact the plane handled correctly, while weights the
Hub cannot address mean the digest is a name rather than a hash of anything. A client that paints
the first as a warning teaches people to ignore the second.

**`POST /v1/artifacts` takes a `source` instead of a `digest`.** Identity is still the digest and
the digest is still immutable — what changed is who computes it. A caller who has to assert the hash
of seventy billion parameters either downloads them first or invents one, and the second is what
actually happened: `hf:org/model` written into the digest field as a name. So the plane derives it.
The caller sends `source`, the plane resolves the manifest, computes the digest, and overlays the
resolved draft underneath whatever the caller stated — `name`, `kind` and `project` win where given,
`metadata` merges with the caller's keys on top, and `digest` and `verified` always come from the
draft, because those two are the plane's answer and not the caller's. Sending both `source` and
`digest` is `409 invalid` rather than a precedence rule: the two disagree or they are redundant, and
neither is worth guessing at. Sending neither is refused too — an artifact without a digest has no
identity — as is a request that resolves to no name, since an artifact nobody can name cannot be
found again.

**The digest is derived from the manifest, not from the bytes.** For a HuggingFace source it is
`sha256` over the newline-joined, sorted `path:oid` lines of the weight files, where `oid` is the
per-file LFS sha256 the Hub already publishes in its file tree. Two properties follow, and both are
why it is defined this way. It costs one API call and not a terabyte of transfer, so registering an
artifact does not mean staging it. And it covers weights only, so a README edit, a retokenized
`tokenizer.json` or a generation-config tweak does not mint a new artifact for the same weights — a
digest that moved every time the repository was touched would fragment the catalogue and defeat the
`existing_id` match it exists to enable.

**`verified` on an artifact is provenance, not identity.** It records that the bytes were resolvable
and content-addressed when the artifact was created: true when every weight file carried an LFS oid
and the digest is therefore a hash of content, false when one did not — an unhashed weight file
means the reference names the weights rather than fixing them, which is also the case for weights
adopted by reference from a running workload. It is set at creation and absent from `PATCH`:
re-verifying an artifact means resolving the bytes again, which is an operation, not an edit.

**`upstream` on an artifact is what the publisher claims, and it is `metadata`'s opposite.** One is
a scratchpad, the other is an assertion somebody signed: the context window, the input modalities,
whether the model reasons and whether it calls tools, as the publisher states them, alongside the
provenance triple — `recorded_by`, `recorded_at`, `source_uri` — that says who said so and from
where. Nothing in it reaches a cluster and nothing in it is checked; a wrong value changes only what
we told somebody. `POST /v1/artifacts` fills it from the resolved source when the caller sends none,
and `PATCH` replaces it whole, the way locations are replaced — a block sent in halves would carry
one claim's provenance over another claim's fields.

`reasoning` and `tool_calling` are tri-state, and null is not `false`. Null is the publisher not
having said, which is the ordinary case for a `config.json`: a workload may still claim either
capability on the strength of an engine recipe that names a parser for it, and collapsing silence
into a denial would block a configuration that works. A `false` is somebody asserting the model
cannot do it, and that does block one.

**`POST /v1/artifacts/{ref}/refresh` is that operation.** It re-reads the artifact's own source and
rewrites only what is derived from it: `format`, `size_bytes`, `licence`, `parameter_count`,
`verified`, and the derived keys of `metadata`. The case it exists for is the plane learning to read
something it did not read before — an attention shape, a native quantization, an active parameter
count — which leaves every artifact registered until then missing it, with no fix but deleting the
artifact (breaking every workload that names it) or retyping the metadata by hand. There is no body:
the source is already on the artifact, and letting a caller name a different one would be a way to
move an artifact onto weights it was not registered from. A `POST` and not a `PATCH` because it
reaches the network and can fail for reasons that have nothing to do with the request.

The response is `{artifact, source, changed, notes}`. `changed` names the fields that actually
moved, dotted for metadata keys (`metadata.shape`); an empty list is a successful refresh that found
the source saying exactly what was already recorded, and no write happens in that case, so nobody's
`If-Match` goes stale because somebody pressed the button. `notes` is the draft's own `{level,
message}` remarks, unfiltered — a repository that has since become gated says so here. Metadata is
overlaid key by key rather than replaced, so labels a person put on the artifact survive; the
derived keys are rewritten even to null, because a key the repository no longer publishes is a fact
about the repository.

`upstream` is refreshed differently again: a re-read fills only the claims that are still blank and
overwrites none of them. The Hub speaking a second time does not get to overwrite what a person
asserted — including the `false` that a resolve never writes.

**A source that now resolves to a different digest is `409 conflict`, never applied.** That is not a
stale record to be corrected, it is different bytes, and different bytes are a different artifact —
writing the new size and parameter count over this digest would leave a row describing weights
nobody can fetch. The detail carries `resolved_digest` and `existing_id`, so a client can offer
registering the new source or point at the artifact that already holds it. An artifact with no
recoverable source — no `metadata.huggingface_id` and no `hf://` location — is `409 invalid`,
because there is nowhere to go back and ask.

**`GET /v1/artifacts/{ref}/fitment` answers what hardware this artifact runs on.** Derived at read
time, stored nowhere, and it reserves nothing: a class it calls a fit still has to survive placement.
Query parameters are `scope` (`fleet` or `catalog`), `context_length`, `concurrency`,
`quantization`, `devices_per_node`, `max_devices` and `limit`. `fleet` asks *what can I run right
now* — only classes a cluster has reported — and `catalog` asks *what would I need*, adding every
class the device table names, so a fit on hardware we own none of is a procurement answer rather
than a bug in the first. `context_length` defaults to the artifact's own declared window, and
`quantization` to however the weights ship; overriding either is how "what if we quantized this" is
asked without registering anything.

Each row carries the layout as well as the verdict: `device_count`, `node_count`,
`devices_per_node`, `tensor_parallel` and `pipeline_parallel`. Nodes are the unit to act on — "four
nodes of B300" is an order and "32 devices" is a number to divide — and a node is however many
devices share a coherent interconnect, eight on an HGX board and seventy-two on a GB300 NVL72.
`tensor_parallel` is bounded by the node because a tensor group over the backend fabric is the one
split that does not work; `pipeline_parallel` crosses freely, so a multi-node replica is normal and
its nodes are one fabric-connected unit rather than a set of islands. `aggregate_gb` is what the
devices *offer*, net of the utilization envelope and the per-device CUDA reserve — multiply
`memory_mib_per_device` by `device_count` for the nameplate, and note that comparing `required_gb`
against the nameplate over-promises by about fifteen percent.

Those fields are the *recommendation* — the fewest cards carrying the full window — and `layouts`
is the rest of the answer: every count on that class that serves, ascending, each with its own
`device_count`, `node_count`, `tensor_parallel`, `pipeline_parallel`, `aggregate_gb`,
`required_gb`, `context_length`, `reduced`, `supply_short` and `reason`. The recommended count is
one of them. Counts below it are the trade of context for cards — four H200 at 32k rather than
eight at 131k is a shape somebody declares on purpose, and it leaves half the node for another
workload — and counts above it stop at one node, or at the recommendation when a replica already
spans several, because widening past the fabric to buy KV cache is a throughput decision nothing
here can price. A class that fits nowhere carries no layouts, and a layout the fleet holds too few
cards for is listed with `supply_short` rather than hidden. `weights_gb` and `kv_counted` are not
repeated per layout: they belong to the artifact and are the same for every entry.

`fits` and `possible` are two verdicts and not one. `possible` is the arithmetic half — the
weights sit on the cards, the attention heads divide across the tensor group — and it is the half a
caller may refuse over, because it is what the plane itself refuses over when the workload is
submitted. `fits` adds the estimated half: the window, the batch and the KV cache, sized against a
flat envelope. A row that is `possible` and not `fits` is the sizing's opinion, and a caller that
turns it into a refusal will eventually refuse a configuration that serves — being wrong that way
is a model nobody can deploy, where being wrong the other way is a worker that fails to start and
reports why. Show such a row with its `reason`; do not disable it.

`sizable` is false only when the artifact carries no parameter count at all; `items` is then empty
and `note` says why. A missing attention shape is not that case — the weights alone still bound
which cards are too small, so the matrix is returned with every row's `kv_counted` false,
`context_length` zero, and `required_gb` a floor rather than a sizing. `considered` is how many
classes were ranked against `items`, which is capped at five unless `limit` says otherwise; the cap
is on the response and not on the ranking, so the head is the best five and not the first five
looked at.

**`/v1/artifacts/{ref}/baselines` is the declared quality floor, one row per dataset and metric.**
Full CRUD — `GET` (filters `dataset`, `metric`), `POST`, `PATCH` on
`/v1/artifacts/{ref}/baselines/{baseline}`, `DELETE` — because a floor is somebody's assertion about
these weights and not something the plane observed. `dataset` and `metric` identify the row within
the artifact and are absent from `PATCH`: moving a floor to another eval is a different baseline. A
second row for a pair already declared is `409 conflict`, since two floors for one eval is two
standards and a verdict would grade against whichever was read first. `recorded_by` and
`recorded_at` are the authenticated principal and the write; a body carrying either is accepted and
the values are discarded, because a signature a caller can type is not one and an acceptance review
argues with the signature. `tolerance_pct` is the recorder's own slack and is not the acceptance
floor — that belongs to the criteria version and is resolved when a verdict is derived (§7.1). The
list is an ordinary paged envelope (`limit`, `cursor`) and `{ref}` is a uid or a name as everywhere
else, while `{baseline}` is the `abl_` uid only. A baseline that belongs to another artifact is `404`
from under this one rather than edited across the boundary: the artifact in the path is part of the
row's identity, not a hint about where to look.

**`cluster` is optional on `POST /v1/workloads`**, and leaving it out is the normal path: the
plane picks the clusters from the workload's device shape, the project's quota, the policies attached
to the workload, and what the clusters report free. Naming one is an override — it is checked for
existence and nothing else, because an operator who names a cluster has said something the scheduler
is not entitled to overrule — and it forces a single deployment. When nothing can hold the workload
the create is `409 conflict` and the body carries a verdict per cluster, each with the numbers it
was rejected on, so "no capacity" is never the whole answer.

**`allowed_regions` and `allowed_clusters` narrow placement instead of replacing it.** They are the
middle ground between the normal path and `cluster`, and the distinction is the same one Kubernetes
draws between `nodeSelector` and `nodeName`: an allow-list still runs placement, still reads quota,
still checks topology and capacity, and still picks — it just has fewer clusters to pick from, so an
allow-list naming a cluster with no devices places nothing while a `cluster` naming the same one is
accepted. `allowed_regions` is a first-class field rather than a cluster list because "any cluster in
`ap`" is the common request and a list of names is one the caller has to maintain as the fleet grows.
Both are allow-lists in the §6 sense: empty means no opinion, never "nothing allowed". Naming a
`cluster` alongside either is `422` — a pin says placement does not decide and an allow-list says it
decides within a smaller set, so letting one silently win would make the other a field that
sometimes does nothing.

What is created is an ordinary **Policy**, named `<workload>-placement`, attached with
`subject_type=workload`, and readable at `GET /v1/policies?subject_type=workload&subject={ref}`.
That is deliberate rather than incidental: placement constraints are Policy, and these two fields
are a shortcut past `POST /v1/policies` rather than a second mechanism beside it. The shortcut has
to exist because placement expands inside the create, so a policy attached after the response has
already missed the decision it was meant to constrain. Being a real policy is what makes the
constraint hold afterwards — it is read back through its attachment on every later expansion, so a
workload scaled past what its region can hold takes a shortfall instead of quietly spilling out of
it — and editing or deleting that policy is how the constraint is changed or lifted.

A create that lands returns the workload with its `deployments` already expanded — one row per
cluster placement chose, each carrying that cluster's share. When the clusters together can hold
only part of the total, the placed shares are returned with the shortfall named rather than the
whole request refused; a partly-running workload is a state the object can express, and refusing it
outright would be the plane deciding that half of what you asked for is worth nothing.

**A deployment carries `status`, which is the cluster's answer rather than the plane's.**
`admission_phase` on the deployment is what the plane decided for this cluster, and `status` holds
what the cluster reports it actually did, including `reason` and `message`: the weights have not
arrived, the cache is still fetching, no node advertises enough accelerators. They are
separate objects because they have separate writers, and reading `admission_phase` alone is how a
deployment appears to be `placed` for the twenty minutes it spends waiting for its weights.
Admission is per-cluster for the same reason: one workload can be running in one region and queued
on quota in another, and a single global phase could not say so. A workload's own `phase` is derived
from its deployments and stored nowhere — `running` when all of them are, `failed` if any is,
otherwise the least advanced among them.

`status` is null until a cluster has reported at all, which is every deployment's first seconds and
the permanent state of one whose cluster cannot reach the hub. Null is therefore an answer — nobody
has said anything — and is not the same as a deployment that is idle. `reason` is a closed set the
operator owns and this API does not narrow: a cluster runs its own build, and a sub-state this plane
predates must reach the screen rather than be rejected on the way in. The set is in `MESSAGES.md`.

**`POST /v1/placement/preview`** asks the same question and writes nothing. It returns the decision
the create would have made, including the shares it would give each cluster. It exists because "why
did it not land in iad-1" is asked before anything is created, and the decision already carries the
reason per cluster.

It takes **exactly one of `workload` and `shape`**, and `project` is required with either. Naming
both, or neither, is `400 invalid` — "a preview names exactly one of workload and shape" — rather
than a precedence rule, because the two forms answer for different subjects and guessing which one
was meant would return a verdict about something the caller did not ask about. `priority_class` and
`replicas` are optional in both forms and override whatever the subject declares, which is what
keeps this a hypothetical rather than a report on a placement that already happened.

```json
{
  "project": "prj_...",
  "shape": {
    "device_class": "h100-80gb-sxm",
    "device_count": 8,
    "device_memory_mib_min": 81920,
    "tensor_parallel": 8,
    "requires_single_topology_domain": false,
    "topology_domain_kind": null
  },
  "priority_class": "standard",
  "replicas": 1
}
```

Only `device_class` is required inside `shape`; the rest default to `1`, `0`, `1`, `false` and
`null` in the order above.

**The shape form exists because the question predates the object.** A create form asks "would this
fit" before anything is committed, and for a while this endpoint could not answer it: narrowing the
subject to a workload — on the argument that with recipes gone there was nothing smaller to name —
was right about the objects and wrong about the question. A recipe used to be able to answer it only
because a recipe was declarable before any workload referenced it, and removing the object silently
removed that property with it. The console found it as a real regression: the verdict panel had
nothing to run against on a form for a workload that does not exist yet.

The fix is not to reinstate an object to be the subject of a question. It is that the question takes
the shape directly, which is what it always wanted. That is strictly better than what stood before
the removal, not merely equal to it — the create form used to get a real verdict only when it reused
an existing recipe, and none at all when it was declaring a new footprint. Both paths get one now.

**`shape` is spelled with the workload's own column names**, deliberately: `device_class`,
`device_count`, `device_memory_mib_min`, `tensor_parallel`, `requires_single_topology_domain` and
`topology_domain_kind` are the fields the object carries, so a preview cannot describe a shape
differently from the object it previews. A second vocabulary here would be a second way to say the
same thing, and the two would drift.

**A shape-form decision is answered under the workload id `wkl_preview`.** It is a deliberate
non-uid: nothing was created, and a plausible `wkl_01m0…` in that field would resolve to nothing for
anyone who looked it up. Clients read it as the marker it is and must not render it as a link.

Common query params: `limit`, `cursor`, `search`, `label` (repeatable, `k=v`), plus the object's
own dimensions (`cluster`, `pool`, `class`, `health`, `phase`, `project`).

## 7. Operations are not verbs on objects

Drain, cordon, reboot, device reset, cache warm, roll, upgrade, benchmark. **`POST /v1/operations`** with a
type and a target — never `POST /v1/nodes/{id}/drain`.

```json
{ "type": "node.drain", "target": "nod_01hq0v...", "reason": "firmware", "deadline": "..." }
{ "type": "node.drain", "target": "node:gpu-node-04", "cluster": "iad-1", "reason": "firmware" }
```

A target is written one of two ways: a prefixed id, or a kind-qualified name — `node:gpu-node-04`,
`device:GPU-8f3c...`, `cluster:iad-1`, `workload:llama-70b`, `deployment:llama-70b`,
`component:operator`, `artifact:llama-3-70b`. The bare name every other path segment accepts is deliberately not a form
here. Elsewhere the path says what kind of object it is addressing; a target does not, and the kind
has to be answerable at creation — `node.drain` against a `dev_...` is a category error caught here
rather than three hops downstream in the operator — so a target reference carries its own kind
either in the id prefix or in the `kind:` half. The same two forms are accepted by the `target`
filter on `GET /v1/operations`.

Names are unique inside a cluster and not above it. A qualified name matching more than one object
is `400 invalid` listing the candidates, never a guess; `cluster` on the create body narrows it
first, and a name matching none is `404 not found`.

**Scaling, pausing and draining a workload are operations.** `workload.scale`
(`{"desired": n, "min": n, "max": n}`), `workload.pause` (`{"paused": bool}`) and `workload.drain`
(`{"cluster": "clu_..."}`) target a workload rather than a cluster's copy of it. Scale is an
operation and not a field write for the reason rule 1 gives: it is long-running and it can fail —
it sits pending for as long as quota withholds the capacity, and "still waiting" is a thing only an
operation can say. `deployment.roll` and `deployment.benchmark` keep targeting a single Deployment,
because rolling and measuring are per-cluster acts.

**Reshaping a workload is an operation too, and it names its new count.** `workload.reshape`
(`{"topology": "aggregated" | "disaggregated", "desired": n, "roles": [{"role": "prefill", "weight": 1}, ...]}`)
flips a workload between one worker and a prefill/decode pair. `roles` is required on the flip to
`disaggregated` and refused on the flip back, which is the same refusal a declared topology already
makes. `desired` is required in both directions and never inherited: the unit under the number
changes with the topology, so a workload at three replicas would silently become three *groups* —
seventy-two devices at 1:2 and TP8. `min` and `max` are optional and default to `desired` for the
same reason. A flip that names the topology already in force is refused; a reshape changes which
processes exist. The result carries the expected unavailability — every worker reloads its weights,
and the peak device count is the sum of the two footprints while the old generation is still up —
so somebody can decide whether to take it now.

These four are **plane-local**: they change declared state and re-run the expansion, and they are
never dispatched to a cluster. `cluster` is therefore absent from a workload-targeted operation
rather than required, and what reaches the clusters is ordinary declared state with different
shares. Every other operation type still needs the cluster its target belongs to.

Returns an Operation with an id and a phase, polled at `GET /v1/operations/{id}`. This follows from
the object model: an operation is long-running, can fail, has an actor and a reason, and needs a
lifecycle of its own. A verb endpoint that returns 200 can express none of that, and the first
question anyone asks about a stuck drain — who started it, when, and why is it stuck — becomes
unanswerable.

### 7.1 Benchmarks are a record, carried by an operation

**`POST /v1/benchmarks`** with a deployment and a harness starts a measurement. It creates two
things: a `BenchmarkRun`, which is the record and the result, and a `deployment.benchmark`
operation, which is how the run is carried out.

```json
{ "deployment": "dep_01hq0v...", "harness": "aiperf",
  "parameters": { "concurrency": 64, "request_count": 500, "output_tokens_mean": 256 } }
```

Two objects rather than one because they answer different questions and have different lifetimes.
The operation has the phases, the deadline, the cancellation and the replay to a cluster that
reconnected mid-run — all machinery that already exists and is not worth building twice. What it
cannot hold is the result: `operation.observed.result` is a flat string map, and a load generator
emits nested JSON. So the numbers land on the run, and stopping a run in flight is
**`DELETE /v1/operations/{id}`** against the operation it names in `operation_id` — there is no
second way to cancel.

**`image` is the harness image the run actually pulled**, recorded on the run and echoed back on
reads. It resolves from the named harness's pinned default unless the creation body overrides it,
which is the escape hatch for reaching a release this plane has not pinned yet. Only the shape of
the reference is validated here — whether it exists is the registry's answer, and a wrong one
surfaces as a pull failure on the run.

**A run's `summary` is the harness's own output and is not normalised.** Two runs are comparable
when their `harness`, `harness_version` and `image` match; across harnesses, or across a release that
redefined a percentile, they are not, and this API does not flatten them into shared columns to
pretend otherwise. A client that reads `summary` has to cope with not recognising it.

**`provenance` is what makes a run worth keeping.** `model`, `deployment_generation`,
`serving_digest` and `engine_args` are snapshotted at creation, not resolved by join: a workload's
serving block is mutable and a deployment is disposable, and a run has to keep describing what it
measured after both have moved on. The digest is stronger than the id and version pair it replaced,
because a digest cannot drift: two runs carrying the same `serving_digest` measured the same
configuration, whatever has since been edited on the workload they came from.

**`GET /v1/benchmarks/serving/{digest}` is the rollup, and it is keyed on the configuration rather
than on the object that declared it.** It answers `serving_digest`, `run_count`,
`graded_run_count` and `peak_output_tokens_per_second` for one `svg_` digest. A digest is what a
measurement is actually about: two workloads declaring the same engine, parallelism and cache
settings over the same artifact run the same process, so keying this per workload would split one
population in half and report each half's best as the peak. It also joins nothing, which is why it
keeps answering after the workload has been renamed, rescaled or deleted — the per-recipe rollup
this replaces could not, because the row it rolled up was mutable and the thing it named could go
away.

**An unknown digest is a zero count, not a `404`.** That is deliberate and it is unlike the rest of
this surface. The question asked here is how much measurement stands behind a configuration, and
"none" is a real answer to it — a digest nobody has benchmarked is not a missing object, it is an
unmeasured one, and a `404` would make a client branch on the difference between the two when only
one of them is interesting.

`peak_output_tokens_per_second` is null when no graded run carried a reading this plane knows how to
find. It is a named absence and never a zero: a configuration that was measured and whose throughput
could not be read is not a configuration that achieved nothing.

**`GET /v1/benchmarks/requests/{ref}/curve` reads a stepped request back as a load curve.** It
answers `request`, `serving_digest`, `step_field`, `criteria_version`, an array of `points` ordered
by `step_index`, and a `knee`. Each point carries the step it was, the run that measured it, its
phase, whether it was discarded, and a `readings` map of `{ "value", "unit", "path" }` — a reading
that did not resolve is a null value beside the path that was looked for, never a zero, because a
zero plotted where nothing was measured is a lie about the hardware. A discarded warm-up stays on
the curve flagged rather than dropped, and a run still open appears with its phase and null
readings, so a ramp can be drawn while it is still climbing.

`output_tps_total` and `request_throughput` are display-only. They are read through the half of the
plane's field map no verdict can see: an aggregate rate clears a per-stream floor under every
possible condition, so a check that could reach one would be a check that can never fail.

**`knee` is always an object and `found` is false more often than it is true.** It names the
`method` (`first-crossing`), the `metric`, and the `threshold` it was measured against, which comes
from the pinned criteria version through the input-length band the steps landed in — never from a
constant in this API. Every false carries its own `reason`: a ramp that never crossed, a ramp that
started above the ceiling, a ramp too short to have a crossing, and a curve that crossed back under
are four different answers. `?criteria=` re-reads the curve against another pinned standard, which
moves the ceiling — the question a stored knee could not answer at all. Nothing here is stored: the
points and the knee are derived at read, exactly as the verdict is.

**`GET /v1/benchmarks/harnesses`** returns each harness with the parameters it accepts, their
types and their defaults. Clients render their form from it rather than carrying a second copy of
what AIPerf takes; a parameter the named harness does not accept is `400 invalid` at creation and
not a job that fails inside a cluster ten minutes later.

There is no `PATCH` and no `DELETE` on a run. A measurement is not editable, and deleting one
discards the history the collection exists to be.

Filters: `deployment`, `cluster`, `harness`, `phase`, `actor`, `since`.

## 8. Writes and idempotency

Mutating requests accept `Idempotency-Key`. `PATCH` is a partial update; a field absent from the
body is untouched, and null explicitly clears. Conditional updates use `If-Match` against the
object's `revision`, and a mismatch is `409 conflict`.

## 9. Status

**Draft.** §1–§5 are settled and clients should be written against them. §6's path list is settled
in shape but individual query dimensions will grow. Payload schemas per resource are not yet
written here — they follow the object model in
[`docs/plans/01`](../../../docs/plans/01-object-model-and-architecture.md) and become binding as each
resource is implemented.
