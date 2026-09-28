# ADR 0004 — Reconciling the HTTP API: CLI and console built against invented shapes

**Status:** accepted · 2026-08-19

## Context

The CLI and the admin console were both scaffolded before `libs/packages/contract/API.md` existed,
each against a shape it invented for itself. They disagreed on several fundamentals, and until one
document settled the disagreement, the two clients' fakes were the only description of the API
that existed — which meant there wasn't one.

## Decision

`libs/packages/contract/API.md` is now that document. Where a client diverged, the client was
wrong, and each disagreement was resolved on the same reasoning the rest of the object model
already uses:

- **Cluster phase vs. reachability** — the CLI kept `phase: pending | active | draining | disabled`
  separate from `reachable`; the console folded reachability and health into phase itself
  (`enrolling | ready | degraded | unreachable | draining`, plus a separate `SyncState`). The CLI's
  shape won. Phase is declared, reachability is observed, and collapsing them makes "the network
  is down" indistinguishable from "someone disabled this cluster" — two facts that need different
  responses from a human and different behaviour from placement, so they cannot share a field. The
  same split now applies everywhere both halves exist: a Component carries `declared_version` and
  `observed_version` as a pair, and a Node carries `cordoned` as an observed boolean rather than a
  value in a condition enum, because draining is an Operation and cordon is the state it leaves
  behind.
- **List envelope** — the CLI's `{items, count, next_cursor}` over the console's
  `{clusters, total}`. One envelope shape reused across every collection beats a name coined per
  resource.
- **Id prefix** — `clu_` over `cls_`, arbitrary between the two but picked and applied
  contract-wide as a single spelling, lowercase Crockford body.
- **`api_endpoint`** — dropped. The CLI never had it; the console exposed it on cluster detail. The
  plane never dials a cluster — the operator dials out and its identity is its credential — so an
  address on the object would be decoration that looks like configuration.
- **`kubelet_version`** — dropped. It was on the console's node shape and was already flagged there
  as leaking Kubernetes into the wire format, which contradicts the rule that the operator is the
  only thing that knows Kubernetes exists.

Both clients independently arrived at the same two patterns without coordinating, and both survived
as written: a declared/observed version pair on components, and a per-device-class rollup rather
than a single device count.

## Consequences

`libs/packages/contract/API.md` is the authority for the HTTP surface; the CLI and the console are
conformed to it, not the other way round. At the time of this decision there was no automated
conformance test for it — the interop harness covers the plane↔operator protocol and
`libs/packages/contract/fixtures` validates ingestion, but the client fakes that exercise this API
are hand-written, so the three-way agreement is by inspection. Nothing in CI would catch the two
clients drifting from `API.md`, or from each other, a second time.
