# 0015: The frontend's router is declared on the workload, as a typed block

**Status:** accepted · 2026-09-11

**Extends:** [ADR 0014](0014-recipe-is-deleted-serving-config-lives-on-the-workload.md), whose serving
configuration gains a block, and [ADR 0013](0013-one-frontend-per-model-routed-by-a-forwarded-deployment-id.md),
whose per-model frontend is what the block configures.

## Context

The operator rendered one router setting onto a model's frontend, `DYN_ROUTER_MODE=kv`,
unconditionally. Some workloads need more than that: a large MoE model can need three further
settings (`DYN_ROUTER_QUEUE_THRESHOLD=1.0`, `DYN_ROUTER_QUEUE_POLICY=wspt`,
`DYN_ROUTER_KV_OVERLAP_SCORE_CREDIT=8.0`) to hold its latency targets under load. With nowhere to
declare them, the only way to run them is a hand patch on the graph deployment with the operator
scaled to zero so nothing renders over it, which is a cluster the plane no longer controls. A
`routing: kv` label on a workload is read by nothing; it was never where the mode came from.

## Decision

1. **`serving.router` is a typed block of four optional fields** (`mode`, `queue_threshold`,
   `queue_policy`, `kv_overlap_score_credit`), each rendered as exactly one `DYN_ROUTER_*` variable on
   the deployment's own frontend. It is stored in its own column, `demand_workload.router`, not inside
   `serving`: that block replaces whole on a patch, and a router setting that vanished when somebody
   edited a parser would undo a benchmark configuration without anyone deciding to.
2. **Undeclared renders what rendered before.** No block, or a block of nulls, is `DYN_ROUTER_MODE=kv`
   and nothing else, so no frontend in the fleet rolls when the plane or the operator learns the field.
   A declared number renders in Python's float spelling, `1.0`, which is also how a hand patch
   typically spells it, so declaring the live values renders the template already running.
3. **The block is in `serving_digest` only while something in it is set.** Every existing digest is
   unchanged, and a configuration benchmarked with a router is a different configuration from the same
   one without it, which is true.
4. **The values are Dynamo 1.5.0's, closed on both sides of the wire.** `mode` is `--router-mode`'s set
   less `direct`, which needs a worker id nothing in front of the frontend sends. `queue_policy` is
   `fcfs | wspt`. Both numbers are finite and `>= 0`. A policy without a threshold, or a KV setting beside
   a non-KV mode, is refused instead of stored, because neither takes effect.
5. **There is no environment passthrough.** The next Dynamo setting that matters becomes a field with
   its own validation, not a string map that can crash-loop a frontend from a text box.

## Consequences

- **Setting a router is a serving change.** It bumps every deployment's generation and the digest, so
  the operator applies the graph once. The block reaches the frontend's pod template and nothing else,
  so a worker is never rolled by it, which is the property that makes this safe on clusters where a
  worker takes hours to load.
- **The plane does not know which Dynamo a frontend runs.** A mode 1.5.0 accepts may not exist in an
  older frontend image, and that frontend will exit at startup. The set is pinned to the release the
  fleet's frontends run and has to be revisited when that changes.
- **Server-side apply keeps what it does not render.** Hand patches the render does not produce (an
  annotation on a pod template, a metadata annotation) survive an apply, and co-owned fields stay
  after the operator stops rendering them. Declaring a router does not clean up the patch it replaces.
