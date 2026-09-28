# 0016 — The listener is the guard; tenant identity replaces the shared token

**Status:** accepted · 2026-09-13

## Context

The gateway's own README used to say "no authentication, metering, or billing," and `Guard`'s doc
comment said "this is not user authentication and must not grow into it." Both were written when
the gateway had exactly one caller — the operator's own inference edge, which authenticates its users
before calling here — and the only question worth asking was whether a request came from that caller
at all. `HANGAR_GATEWAY_TOKEN` answered it: a shared secret, checked in constant time, with no notion
of who was on the other end beyond "holds the current token."

A second tenant changes the question. A tenant that calls the gateway directly, rather than through
the operator's edge, means the gateway now has to know *which* tenant a request belongs to, not only
whether it is allowed to ask at all. A shared secret cannot answer that — every holder of
`HANGAR_GATEWAY_TOKEN` looks identical to the hop that checks it — and widening `Guard` to carry a
tenant name per token would have turned the one thing its own doc comment forbade into the thing it
became: a credential store, and the wrong shape of one, since revoking one caller would mean
rotating a value every other caller of the same tenant also holds.

The tension was visible before it was resolved: a credential that named a tenant at that hop would
make it the credential store its own `Guard` doc says it must not be. This decision is the
resolution.

## Decision

1. **Which socket accepted the connection is the trust primitive, not a header any caller can
   send.** `internal/serving.Zone` is fixed at listener construction — `ZoneInternal` for the
   existing ClusterIP-with-no-ingress port, `ZonePublic` for a new listener behind a
   `*.hangar.example.com` wildcard. A caller cannot forge which port it dialled, which is exactly the
   property `Guard` was trying to approximate with a secret that *could* be copied.
2. **Tenant identity comes from `Host`, checked only where the zone requires proof.** On the
   internal zone, reaching the port is the proof and a tenant may declare `auth: none` — this is the
   shape of an operator whose own edge authenticates its users, and nothing about how that edge
   calls the gateway changes. On the public zone, the `Host` header resolves a tenant and, when that
   tenant declares `auth: api_key`, a presented key's hash must belong to *that* tenant specifically —
   not to some tenant, which is what would let a key stolen from one tenant authenticate as another.
3. **`Guard`, `TokenGuard` and `HANGAR_GATEWAY_TOKEN` are deleted, not deprecated.** Their entire
   job — is this hop being called by the system in front of it — is now answered by the zone. A
   shared secret checked in front of both zones alike would have been a weaker claim sitting beside
   a stronger one for no reason.
4. **Routing is scoped by tenant on the public zone only, and a stranger's model is `404`.**
   `routing.Route.TenantID` rides the table the same way `Policy` does — refreshed on a timer, never
   fetched per request. Resolving a name that belongs to another tenant is treated exactly like
   resolving a name that does not exist: the plane's own `ResolvedEndpointSchema` doc comment makes
   the same argument for the same reason, and a `403` here would confirm the endpoint's existence to
   a caller who has no business learning that.
5. **The tenant and key stages are unconditional no-ops until `ZonePublic` exists.** Every stage that
   reads `Zone` or `Tenants` checks `zone == ZonePublic` first, and a handler is only constructed with
   that zone when the public listener is configured. The stages are wired into `withRouting`'s
   pipeline and into [`cmd/gateway/main.go`](../../libs/gateway/cmd/gateway/main.go)'s boot sequence
   (the tenant table loads and refreshes exactly like the routing table) in the same change that
   deletes `Guard`, but activating them is `HANGAR_GATEWAY_PUBLIC_LISTEN` naming an address, which
   stays unset until an operator decides to accept direct tenants.

## What this does not change

**Metering and billing stay out.** The gateway still counts nothing for an invoice; identifying a
tenant is not billing it. If a direct tenant is ever billed on usage, where that number comes from
is a separate decision, and nothing here reads or writes a usage ledger.

**User identity below the tenant is still not this hop's business.** A tenant's own users are
authenticated by whatever fronts that tenant's own traffic before it reaches here, exactly as an
operator's edge already does for its users. `auth: api_key` identifies a *caller*, which is a
tenant-scoped credential the operator hands out — not a person.

**Rate limiting is still `ServingQuota`'s job, not this one's.** This decision is a prerequisite for
it — `ServingQuota` enforcement needs an identified caller and a place in the pipeline to sit, and
the zone/tenant stages provide both — but no limit is enforced here.

## Consequences

- **A key is checked against its own tenant, never against the set of all valid keys.** This costs
  one extra map lookup per request on the public zone (`TenantByKeyHash`, compared against the
  tenant `TenantByHost` already resolved) and buys the one property a flat set of valid keys cannot:
  a key minted for one tenant can never authenticate as any other, no matter how it leaked.
- **An existing edge needs no change at all.** It stays `internal` + `none`, dialling the same
  in-cluster hostname it always has. It can drop `HANGAR_GATEWAY_TOKEN` from its config once the
  gateway that no longer checks it has deployed — an env var a running binary never reads is inert,
  not a step that has to happen first.
- **A gateway with `HANGAR_GATEWAY_PUBLIC_LISTEN` unset boots and serves exactly as it did before
  this change**, apart from one new startup dependency: the tenant table's first load must succeed,
  the same posture the routing table already has, and for the same reason — coming up with nothing
  loaded would mean the stages that read it have never seen a tenant at all.
