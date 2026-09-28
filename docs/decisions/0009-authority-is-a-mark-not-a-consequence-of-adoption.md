# ADR 0009 — Authority is an explicit mark, not a consequence of how an object arrived

**Status:** accepted · 2026-08-21 · the object named throughout amended by ADR 0014

**Amends:** [0006](0006-adoption-is-a-decision-to-observe.md), which made "was this adopted?" the
question that decided whether the operator may act. This ADR replaces that question with an explicit
one and keeps everything else 0006 decided.

## Context

0006 was right about the cost it was avoiding and wrong about where to attach the rule. It made
adoption permanent: `reconcileAdopted` refuses to apply, always, because the object was adopted. The
refusal is correct for a workload we are only watching and wrong for one somebody has since decided
the plane should run, and there was no way to say which was which — the provenance of the object had
become a standing prohibition on ever managing it.

Three things went wrong downstream of that, and they are the same mistake seen from different sides.

**Fidelity got tied to authority.** The operator is a process inside the cluster with API access. It
can read every argument, every env name and every resource request off a running pod whether or not
the plane manages it, and it already does — `workload.discovered` carries `spec` in full. But the
only thing the adoption path *kept* was `spec_fingerprint`, a digest. A digest answers "did this
change?" and cannot answer "what is it?", which is the right primitive for a drift gate and the
wrong one for an inventory. We had the data and threw it away at the door.

**The import was lossy, silently.** `derive()`
([`libs/plane/app/modules/discovered/derive.py`](../../libs/plane/app/modules/discovered/derive.py))
maps a container's argv onto recipe fields and drops what it has no column for. Worse than dropping,
it mis-mapped: `--max-num-seqs` was read as `max_batch_tokens`, which is `--max-num-batched-tokens`
and a different knob entirely. Every recipe drafted from a running vLLM workload carried that error.
It was invisible precisely because adopted recipes are never rendered — a recipe that cannot take
effect cannot be caught being wrong by anything except a person reading it.

**"Adopted" was doing two jobs.** It recorded where an object came from, and it decided what the
operator was allowed to do. Those change independently. A workload is adopted forever — that is a
historical fact — but whether we manage it is a decision someone makes on a Tuesday and may reverse.

## Decision

**Authority is a field, it is binary, and it is on the deployment.**

    authority: observed | managed

- `observed` — the plane records this workload and verifies it against what it was pinned to. It
  writes nothing to the cluster, ever. This is what every promoted deployment is on day one and what
  0006 called adopted.
- `managed` — the plane renders and converges it, exactly as it does for a deployment it created.

The operator gates on `authority`, not on the presence of `adopt`. `adopt` stays and keeps its
meaning — the live object's identity and the digest it was pinned to — but it is now provenance and
a drift check rather than a prohibition.

**Observation is lossless and does not consult authority.** The full observed `spec` is recorded for
every sighting regardless of what we intend to do with it, alongside `spec_fingerprint`. Two fields,
two jobs: the spec describes, the digest compares. Neither is derived from the other and neither
replaces the other.

**Imported parameters are recorded separately from declared ones.** The recipe carries
`imported_args` beside `engine_args`:

- `engine_args` is **declared**. A human asserted it. It renders onto the command line.
- `imported_args` is **observed**. It was read off a running process. It is recorded in full, it is
  never rendered, and it is never enforced.

The split is provenance, and provenance is what makes promotion reviewable. Folded into one map you
cannot tell intent from incidental capture, and at the moment you first render the recipe you are
guessing which values somebody meant. Kept apart, becoming managed is a real act: here is everything
we read, here is the subset we are now asserting, and the difference is visible before it is applied.

**Nothing about `imported_args` is a promise to reproduce it.** It is a record of what was seen. A
parameter we do not model and do not declare will not appear on a rendered command line just because
we wrote it down, and a reader can see that without running anything.

## Consequences

**Managing a workload becomes possible, and stays deliberate.** `PUT /v1/deployments/{ref}/authority`
is the transition, in both directions. Going to `managed` is the one that can cost a restart, so it
is the one that requires the caller to send the fingerprint they were shown, the same gate promotion
uses. Going back to `observed` is free and always allowed: it is the plane letting go, and letting go
never needs to touch the cluster.

**Promotion can be non-restarting, and that is now a property worth defending.** If the declared set
renders to what is already running, converging is a no-op and taking management costs nothing. That
only holds because imported and declared are separate — it is checkable, because both halves are
written down. Where it does not hold, the diff is the honest price of the change somebody asked for,
which is the distinction 0006 set out to protect and this ADR keeps.

> **Amended 2026-09-02.** Recipe is deleted (ADR 0014) and its columns sit on the Workload, so
> `imported_args` and `engine_args` are now two fields of the workload's serving configuration
> rather than two fields of a recipe. Nothing this ADR decided moves with them: the split is still
> provenance, `imported_args` is still never rendered and never enforced, and `derive()` still
> drafts one from a sighting. The removal only strengthens the line — `serving_digest` is computed
> over the declared half and deliberately excludes `imported_args`, so what was merely observed
> cannot change the identity of what runs.

**A mis-mapped import is now a bug with consequences rather than a dormant one.** Recipes that can
render are recipes that can be wrong out loud. `--max-num-seqs` is fixed to flow to `imported_args`
where it belongs, and `max_batch_tokens` means `--max-num-batched-tokens` and nothing else. Recipes
already drafted before the fix carry the old error and are not migrated silently — a data fix is a
decision about a serving parameter, not a schema change.

**There is deliberately no partial authority.** A deployment where some fields are enforced and
others drift is unanalyzable at exactly the moment somebody needs to reason about it, so the object
answers "who owns this?" with one word. Per-field ownership is the right representation underneath if
we ever need it — Kubernetes server-side apply's `managedFields` is the prior art and exists because
this is genuinely hard — but the object-level answer stays binary.

**`observed` remains the default everywhere.** Promotion creates `observed`. An unset authority on an
old row reads as `observed`. Nothing acquires management by omission, which is the one property 0006
got exactly right and this ADR is careful not to spend.
