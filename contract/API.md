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
ten_ prc_ plc_ alc_ wkl_`.

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
| `/v1/pools` | declared, with observed membership |
| `/v1/artifacts`, `/v1/recipes`, `/v1/deployments`, `/v1/endpoints` | declared |
| `/v1/discovered` | observed, read-only, plus two decision verbs — see below |
| `/v1/replicas`, `/v1/allocations` | observed, read-only |
| `/v1/projects`, `/v1/quotas`, `/v1/priority-classes`, `/v1/policies` | declared |
| `/v1/components` | declared version, observed rollout |
| `/v1/operations` | see §7 |
| `/v1/events` | observed, read-only, append-only |

Sub-resources are addressed by filter, not by nesting: `GET /v1/devices?cluster=iad-1`, not
`/v1/clusters/iad-1/devices`. One collection per object type keeps filtering, pagination and
permissions in one place instead of N.

**`/v1/discovered` is the one observed collection that accepts a `POST`**, and the exception proves
the rule: `POST /v1/discovered/{ref}/track` and `.../ignore` do not write the sighting, which stays
ingestion's alone. They write a decision beside it — one row saying we looked at this and want it in
the inventory, or do not. Neither produces a `deployment.desired`, and that is the guarantee the two
verbs exist to make: declaring a workload that is already running is how a control plane restarts
it, and reloading a model into VRAM costs minutes of served traffic. Adoption is a decision to
observe, never a decision to declare. `?status=new|tracked|ignored` filters on the decision.

Common query params: `limit`, `cursor`, `search`, `label` (repeatable, `k=v`), plus the object's
own dimensions (`cluster`, `pool`, `class`, `health`, `phase`, `project`).

## 7. Operations are not verbs on objects

Drain, cordon, reboot, device reset, cache warm, roll, upgrade. **`POST /v1/operations`** with a
type and a target — never `POST /v1/nodes/{id}/drain`.

```json
{ "type": "node.drain", "target": "nod_01hq0v...", "reason": "firmware", "deadline": "..." }
```

Returns an Operation with an id and a phase, polled at `GET /v1/operations/{id}`. This follows from
the object model: an operation is long-running, can fail, has an actor and a reason, and needs a
lifecycle of its own. A verb endpoint that returns 200 can express none of that, and the first
question anyone asks about a stuck drain — who started it, when, and why is it stuck — becomes
unanswerable.

## 8. Writes and idempotency

Mutating requests accept `Idempotency-Key`. `PATCH` is a partial update; a field absent from the
body is untouched, and null explicitly clears. Conditional updates use `If-Match` against the
object's `revision`, and a mismatch is `409 conflict`.

## 9. Status

**Draft.** §1–§5 are settled and clients should be written against them. §6's path list is settled
in shape but individual query dimensions will grow. Payload schemas per resource are not yet
written here — they follow the object model in
[`docs/plans/01`](../docs/plans/01-object-model-and-architecture.md) and become binding as each
resource is implemented.
