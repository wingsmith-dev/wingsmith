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

## 2. Identity and scope

- `Authorization: Bearer <token>` — **stubbed**. One static token, no user auth, no rotation.
- `X-Hangar-Project: prj_...` — scopes the request to a project.

Auth is deliberately deferred, but every handler resolves a caller identity from day one even
though it is always the same one. The seam is one function; without it, adding auth later means
touching every handler instead of one.

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
| `/v1/replicas`, `/v1/allocations` | observed, read-only |
| `/v1/projects`, `/v1/quotas`, `/v1/priority-classes`, `/v1/policies` | declared |
| `/v1/components` | declared version, observed rollout |
| `/v1/operations` | see §7 |
| `/v1/events` | observed, read-only, append-only |

Sub-resources are addressed by filter, not by nesting: `GET /v1/devices?cluster=iad-1`, not
`/v1/clusters/iad-1/devices`. One collection per object type keeps filtering, pagination and
permissions in one place instead of N.

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
