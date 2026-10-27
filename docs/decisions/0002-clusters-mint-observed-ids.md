# ADR 0002: The cluster mints the ids of the things it observes

**Status:** accepted · 2026-08-19

## Context

Ingestion had to decide where a `nod_`, `dev_` or `tpd_` id comes from, and the contract did not
say. It resolved the ambiguity by adopting a correctly-prefixed id off the wire and minting one
otherwise: a heuristic, not a rule, and one that quietly made the operator the authority for ids
the plane publishes through its own HTTP API.

Two coherent positions were available:

1. **The cluster mints.** `node.observed` carries its own `nod_` id; the plane stores it verbatim.
2. **The plane mints.** The wire references observed rows by the natural keys the tables already
   enforce (`name` for a node, `device_uuid` for a device, `kind` + `identifier` for a domain)
   and the plane assigns a public id on first sight.

## Decision

The cluster mints. The plane validates the shape, stores the id as given, and never re-mints it:
`uid` is written on insert and appears in no update set, so a row rediscovered after a reconnect
keeps the id it was first given.

## Why

An observed object needs a handle from the moment it is discovered, and the plane cannot supply one
for a thing it has not yet heard of. Under option 2 the operator still has to name a node in a
`device.observed` frame that may arrive before the plane has ever seen that node, so the natural
key ends up carrying identity anyway, and the minted id becomes a second identity layered on top of
a first one that already works. One identity is better than two that must be kept in agreement.

The objection to option 1 is real but narrow: ids in the plane's public API are then chosen by a
component the API does not control, so a buggy operator can churn or collide them. That objection
is weaker here than it would be generally, because **whoever runs the plane also runs the operator
in every cluster it manages**: the operator is the same project's code, shipped alongside the
plane, and its ids are ULIDs whose collision probability is not the thing that will go wrong first.
Prefix and shape are validated on receipt, and the unique constraint on `uid` is the backstop.

## Consequences

- Id shape is contractual, not cosmetic. `libs/packages/contract/MESSAGES.md` states it.
- A frame carrying a malformed id is a **poison frame**: it can never be applied, so withholding
  the ack would replay it forever. It is accepted, discarded and logged instead, per PROTOCOL §6b.
- If the plane is ever pointed at an operator it does not ship, a third-party agent reporting into
  it, this is one of the decisions to revisit, along with `Pool`, which has the same assumption
  baked in.
