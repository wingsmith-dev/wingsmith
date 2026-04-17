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
  returns `{"token": "...", "expires_in": <seconds>}`; that token, not the password, is the bearer
  credential on every request after. It is short-lived and signed, and expires without a rotation
  call — log in again. Sending the password itself as a per-request credential is deliberately not
  supported: it would end up in proxy logs and could not be rotated independently of the person who
  knows it.

A request with no token, an unrecognized token, or an expired or tampered session token gets
`401 not_authorized` in the standard error envelope. `/health` is exempt so probes do not need
credentials.

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
  references. Generic observed attributes (os image, kernel version, driver version, agent version)
  are fine. The operator is the only thing that knows Kubernetes exists; leaking it here would make
  the plane's API a Kubernetes API with extra steps.
- **No telemetry.** Utilization, temperature, throughput, latency, token counts. Health, allocation,
  membership and residency are state and belong here; measurement goes to the observability stack
  directly.

## 6. Resources

Declared objects take the full set: `GET` list, `GET` one, `POST`, `PATCH`, `DELETE`.
Observed objects are **read-only** — there is no `POST /v1/devices`, because devices are discovered,
not created. Writing to an observed resource is a category error and returns `409 invalid`.

| Path | Kind |
|---|---|
| `/v1/clusters` | declared registry, observed body |
| `/v1/nodes`, `/v1/devices`, `/v1/topology-domains` | observed, read-only |
| `/v1/device-classes` | the shared dictionary, read-only — see below |
| `/v1/pools` | declared, with observed membership |
| `/v1/artifacts` | declared, plus one read-only resolve verb — see below |
| `/v1/recipes`, `/v1/workloads`, `/v1/endpoints` | declared, plus a read-only resolution — see below |
| `/v1/deployments` | declared, per-cluster, read-mostly — see below |
| `/v1/discovered` | observed, read-only, plus the decision verbs — see below |
| `/v1/replicas`, `/v1/allocations` | observed, read-only |
| `/v1/tenants`, `/v1/projects`, `/v1/quotas`, `/v1/priority-classes`, `/v1/policies` | declared |
| `/v1/components` | declared version, observed rollout |
| `/v1/placement` | one dry-run verb, writes nothing — see below |
| `/v1/operations` | see §7 |
| `/v1/benchmarks` | declared run, observed result — see §7.1 |
| `/v1/events` | observed, read-only, append-only |
| `/v1/alerts` | derived, read-only apart from two lifecycle verbs — see below |
| `/v1/notification-channels` | declared, plus a `test` verb |

**`GET /v1/endpoints/resolved` is derived and belongs to no object.** An endpoint declares a name
and a set of targets; the resolution is what that means right now — each enabled target followed
to its deployment, that deployment's cluster, and the serving address the cluster carries. It is
computed on read and stored nowhere, because a materialised copy is a second version of the same
fact with its own way of being stale. The whole table comes back unpaginated: a caller that
assembled half a routing table would route half its traffic into a hole. It is `ETag`ged by a
revision covering all four tables it was built from, so polling it costs a `304` until one of them
moves. An endpoint with no usable targets is returned with an empty upstream list rather than
omitted — "no such model" and "this model has nowhere to go" are different answers.

Sub-resources are addressed by filter, not by nesting: `GET /v1/devices?cluster=iad-1`, not
`/v1/clusters/iad-1/devices`. One collection per object type keeps filtering, pagination and
permissions in one place instead of N.

**`/v1/device-classes` is a dictionary, which is a fourth kind alongside declared, observed and
derived.** A device class is the identity a GPU model is placed against — `h100-80gb-sxm` on a
pool, a recipe shape, a quota and a device — and it is defined in `devices.json` in this package,
read by the plane and embedded in the operator. Nothing declares one and no cluster reports one, so
there is no `POST` and no `PATCH`: adding hardware is a change to the contract table and a release
of both halves. Editing it over the API would leave the plane and the operator resolving vendor
strings against different dictionaries, which is the one thing a shared table exists to prevent.

Each entry carries `name` — the identifier, and the only string that travels on the wire — and
`label`, the only string to put in front of a person. Alongside them is `inventory`: how many
devices of that class the fleet holds, how many are healthy, how many are allocatable, and which
clusters they are in. Counted from observed state on read, never stored.

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

Set them on the **workload**, not the recipe: a recipe is shared and versioned, so muting there
would quieten every workload of that model at once. Labels are a workload fact and there is no
per-cluster override of them; muting a workload mutes it in every cluster it runs in, which is what
a workload-level knob should mean. Cluster conditions read the cluster's labels,
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

**`/v1/workloads` is what you create; `/v1/deployments` is what that became.** A Workload is the
global declaration — a recipe, a total scale, a kind, a priority, and the regions or clusters it may
use. A Deployment is that workload on **one** cluster, and it is still exactly what the operator
reconciles. "Run gpt-20b on asia and europe" is one workload, two deployments, one replica each.
Nothing about a Deployment's meaning changed; a layer was added above it.

The split follows the fact, not the object: what is true of the whole workload (recipe, adapters,
overrides, labels, kind, priority class, preemptible, gang size, deadline, the topology-domain pair,
and the min/max/desired totals) lives on the Workload, and what is true of one cluster (admission
phase and reason, the queued/admitted/placed timestamps, generation, enabled, authority, adoption)
lives on the Deployment.

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

**`GET /v1/discovered/{ref}/recipe-draft` reads a sighting as the objects needed to adopt it.** A
`GET` because it is one: the same sighting derives the same draft every time, and asking changes
nothing. It returns the proposed recipe — engine and version from the image tag, context length and
parallelism from the container's argv — and, under `artifact`, the weights that recipe would point
at. The draft's `imported_args` is the whole argv as flag/value pairs, including arguments that also
populated a first-class field, because a record that keeps only what it has columns for cannot be
checked against the cluster by the person confirming it. Its `engine_args` is always empty: reading a
flag off a running pod records it, and asserting it is a separate act by a person. The artifact half exists because a recipe cannot be written without one and a workload the plane
never declared has no artifact anywhere: `--model org/model` becomes an `hf://` source and a
`digest` of `hf:org/model`, with the revision appended when `--revision` was given. That digest is a
name, not a hash, so the artifact is written with `verified: false` and stays that way until
something resolves the bytes. `artifact.existing_id` is filled when the catalogue already holds
those weights, which makes adopting one a confirmation instead of a duplicate. Nothing on this path
writes; `POST /v1/artifacts` and `POST /v1/recipes` do, on their own modules' endpoints, so there is
one way to create each object rather than two that must agree.

**`GET /v1/artifacts/resolve?source=hf://org/model` answers what an artifact would be.** A `GET`
for the same reason `recipe-draft` is one: the same source resolves to the same answer every time,
and asking changes nothing — no row is written, no id is minted, and calling it twice costs nothing
but a Hub round trip. It returns the draft the create would have built: the derived digest, the
name, format, size, licence and parameter count, the source locations, a `recipe` block of suggested
device count and context length that is a suggestion and is never applied by anything, and
`existing_id` when the catalogue already holds that digest. It is a preview endpoint and not a
second door into the catalogue: `POST /v1/artifacts` remains the only way to create one, so the
claim above — one way to create each object rather than two that must agree — still holds. Unknown
slug is `404 not found`; a gated repository is `409 invalid` naming the gate and the token variable
to set, because the fix is a credential and not a retry; a malformed source is `409 invalid`; the
Hub being down or refusing is `500`, since the failure is upstream of us and the caller's request
was fine.

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

**`cluster` is optional on `POST /v1/workloads`**, and leaving it out is the normal path: the
plane picks the clusters from the recipe's device shape, the project's quota, the policies attached
to the workload, and what the clusters report free. Naming one is an override — it is checked for
existence and nothing else, because an operator who names a cluster has said something the scheduler
is not entitled to overrule — and it forces a single deployment. When nothing can hold the workload
the create is `409 conflict` and the body carries a verdict per cluster, each with the numbers it
was rejected on, so "no capacity" is never the whole answer.

A create that lands returns the workload with its `deployments` already expanded — one row per
cluster placement chose, each carrying that cluster's share. When the clusters together can hold
only part of the total, the placed shares are returned with the shortfall named rather than the
whole request refused; a partly-running workload is a state the object can express, and refusing it
outright would be the plane deciding that half of what you asked for is worth nothing.

**A deployment carries `status`, which is the cluster's answer rather than the plane's.**
`admission_phase` on the deployment is what the plane decided for this cluster, and `status` holds
what the cluster reports it actually did, including `reason` and `message`: the recipe has not
arrived, the weights are still downloading, no node advertises enough accelerators. They are
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

**`POST /v1/placement/preview`** asks the same question and writes nothing. It takes a recipe, a
project, an optional priority class, a kind, a strategy and a total scale — or an existing workload
reference — and returns the decision the create would have made, including the shares it would give
each cluster. It exists because "why did it not land in iad-1" is asked before anything is created,
and the decision already carries the reason per cluster.

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

These three are **plane-local**: they change declared state and re-run the expansion, and they are
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

**`provenance` is what makes a run worth keeping.** `model`, `deployment_generation`, `recipe_id`,
`recipe_version` and `engine_args` are snapshotted at creation, not resolved by join: a recipe is
mutable and a deployment is disposable, and a run has to keep describing what it measured after
both have moved on.

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
