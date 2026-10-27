# Method

`README.md` covers the load generators and what `hgrbench` does with them. This is the
other half: how to spend a session measuring a serving deployment and know what you
learned at the end of it. None of it is enforced by the tooling. It is a checklist for
the person running the tooling, written to be model-, engine- and topology-independent.

## The rule

**A run is the last step, not the first.** Before firing one:

1. A number can close the question, and you have written down which number and which
   value would falsify what you expect.
2. No cheaper step can produce it.
3. A comparator exists differing in exactly one thing, measured on the same fleet.
4. The expected effect is larger than this deployment's **measured** noise floor.

Point 4 is where a global constant does the most damage. There is no universal
threshold: noise is a property of a deployment and a metric, it has to be measured from
repeats that **cross a reload** of the serving workers, and until it has been, no
difference can accurately be called real or null.

## Steps, sorted by what they actually cost

The expensive thing is usually not GPU time, it is **worker reloads**. Weights take
minutes to load and a rollout often restarts a whole fleet, so a benchmark against an
already-loaded deployment can be cheaper than a one-worker experiment that needs a fresh
load. Sort by that, not by device count.

| step | costs | answers |
|---|---|---|
| **0: arithmetic** | seconds | Working set against cache capacity, session ceiling, batch-budget utilisation. Read capacity from the engine's own configuration instead of modelling it. |
| **1, no reload** | one cell | Anything the client or the router controls: concurrency, request shape, request count, routing policy, arrival rate. Ask every question of this kind against the current fleet before rolling it. |
| **2: one reload** | a load plus a cell | Any engine flag or image change. This is the real unit of cost, and an experiment queue should be ordered around it. |
| **3: a curve** | hours | Ladders and saturation searches, once step 0 has said where the cliff should be. |

Fewer devices is worth asking for and does not always answer the question. When
capacity scales with worker count and capacity is the binding constraint, a one-node
proxy changes the thing under test, so it cannot give a *fleet* throughput number. It
can still rank two configurations at matched cache pressure, and ranking is all an A/B
comparison needs.

## Write down what a run assumed

A number is only comparable to another number measured under the same conditions, so
record them next to it: the image and its version, the rendered engine arguments, the
worker generation, the request shape, and the plan that produced the run. `hgrbench`
keeps the plan path, the harness image digests and every harness export in `suite.json`;
the deployment side is yours to record. When the live fleet differs from what a past
run recorded, that run is evidence about a fleet that no longer exists.

## Windowing: the telemetry dump is not the run

When server-side metrics are read beside a run, pin the analysis window to the run
instead of inferring it from the file. Load generators record their own start and end
times, so the intersection is exact. A window found by matching request counts, or by
picking the longest active stretch of a dump, is a guess about which benchmark it was,
and a number from it should be labelled as one.

Two arithmetic rules matter here. Take counter deltas **per pod and then sum**:
differencing a summed counter goes negative whenever one scrape fails, and taking one
pod's delta as the role's under-reports a multi-pod role. And compare arms on
**absolute elapsed time**, not on each run's own fraction, because a slower run's second
half sits at a different point in the workload.

Keep per-pod figures in the summary. A role total can hide an imbalance completely: one
worker holding a long queue while its peer idles looks, at role level, like a cache
problem.

## Blocks

Attribute a run to one constraint before choosing the next experiment. The thresholds
come from the deployment; only the shape of the table is general.

| block | metric | binding when |
|---|---|---|
| admission | running vs the sequence cap, queue time | running is pinned *at* the cap; then the cap is the fix, not more nodes |
| prefix cache | hit rate, working set vs measured capacity | hit rate falls during the run |
| prefill compute | batch fill against the token budget | budget full and the queue non-zero |
| transport | KV transfer time vs the same fleet's healthy figure | it moves by orders of magnitude; a fixed absolute cut-off near the healthy figure flags a healthy fleet |
| decode block capacity | blocks per request vs pool, preemption cascades | preemptions cascade with the token budget unfilled; the fix is usually the watermark, not the batch size |
| decode interference | per-user output rate vs a disaggregated reference | latency several times worse while cache, queue and preemptions are all clean |
| load balance | least-loaded worker's share of the busiest | one worker queues while its peer idles |
| SLO | the workload's targets | throughput is fine and the target is missed anyway |

The next experiment comes from whichever block is binding, not from the list of untried
flags. Tuning batch budgets and sequence caps while a different block is binding
produces a long table of rows that cannot move.

## Order of work

1. **Read what is free.** Capacity, configured limits, the rendered arguments.
2. **Baseline across a reload,** A/B/A. Repeats taken back to back on one generation of
   workers give a noise floor that is too small, and a floor that is too small turns
   noise into findings. Keep a control arm on every generation.
3. **Everything that needs no reload,** against the fleet you already have.
4. **Then the reload-bound queue,** ordered by what buys the most reloads back.
5. **Write down the nulls and the voids.** Negative results are most of what a session
   learns.

## Stop rules

- Three consecutive void or null results: check the instrument, not the config.
- A question still open after two runs: wrong step. Drop to step 0 or 1.
- Any number that surprises you: re-derive it on a second instrument before building on
  it. Client-side and server-side request rates for the same run should agree closely;
  when they do not, one of the instruments is wrong.

## What transfers, and what does not

The **failure catalogue** transfers: under-firing, a session ceiling, a fleet halfway
through a rollout, a telemetry sampler that outlived its run, the wrong analysis window,
per-pod aggregation, an unmatched shape between arms, a flag that was already set,
transport silently degraded, overlapping runs, load imbalance within a role.

These do **not** transfer and belong in notes about one deployment:

- Prefix-cache dominance is a property of the agentic multi-turn shape, not of a model.
- Whether tensor parallelism shards the KV cache depends on the attention variant: it
  does for grouped-query attention, and does not for MLA-style attention, where the
  latent cache is replicated. Reading capacity from the engine makes this irrelevant.
- Disaggregated prefill and decode exist only in some deployments. Detect roles from
  behaviour, not from pod names, so an aggregated fleet reads correctly even when
  its pods are named as though it were not.
