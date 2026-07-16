# Method

`README.md` covers the load generators — the client half. This is the other half: how to
spend a night measuring a serving deployment and know what you learned at the end of it.

It is written to be model-, engine- and topology-independent. Everything model-specific
lives in a profile, and the tooling holds no numbers at all. The worked example
throughout is a Kimi-K3 disaggregated deployment, because that is where the failures were
learned; none of the conclusions from it are baked in.

## Why this exists

A benchmarking session on one model ran about 24 hours and ended without confidence in
its results. The runs were not the problem. Judged mechanically afterwards, 19 of 24
archived runs were void, every one for a defect visible in its own artifacts at the time
— arms fired at different request counts, concurrency above the session ceiling, a fleet
halfway through a rollout, analysis windows that covered a different benchmark entirely.
Each was found by hand days later, after more runs had been built on top of it.

Two of the instruments were also wrong, which is worse than any individual run being
wrong. A telemetry summariser reported one worker's throughput as the whole role's for
the life of the project. Sampler processes outlived their benchmarks and kept scraping
into the next one, so ten of seventeen telemetry dumps held more than one run and the
largest held eleven across five worker generations — and the tool that read them picked
whichever stretch was longest.

## The rule

**A run is the last step, not the first.** Before firing one:

1. A number can close the question, and you have written down which number and which
   value would falsify what you expect.
2. No cheaper tier can produce it.
3. A comparator exists differing in exactly one thing, measured on this fleet.
4. The expected effect is larger than this deployment's **measured** noise floor.

Point 4 is where a global constant is fatal. There is no universal threshold: it is a
property of a deployment and a metric, it must be measured from repeats that **cross a
reload**, and until it has been, no difference can honestly be called real or null.

## Tiers, sorted by what they actually cost

The expensive thing is not GPUs, it is **worker reloads**. Weights take minutes to load
and the operator usually rolls a whole fleet, so a benchmark against an already-loaded
deployment is often *cheaper* than a one-worker experiment that needs a fresh load. Sort
by that, not by device count.

| tier | costs | answers |
|---|---|---|
| **0 — arithmetic** | seconds | Working set against capacity, session ceiling, batch-budget utilisation. Capacity is **read from the engine**, never modelled: one study here found a vendor capacity simulator off by 18–40% on the fleet it was simulating. |
| **1 — no reload** | one cell | Anything the client or the router controls: concurrency, shape, request count, routing policy, arrival rate. Run *every* question of this kind against the current fleet before rolling it. |
| **2 — one reload** | a load plus a cell | Any engine flag or image change. This is the real unit of cost, and it is what an experiment queue should be ordered around. |
| **3 — a curve** | hours | Ladders and saturation searches, only once tier 0 has said where the cliff should be. |

Fewer devices is worth asking for and is not always available. When capacity scales with
worker count and capacity is the binding constraint, a one-node proxy changes the thing
under test — so it cannot give a *fleet* throughput number. It can still rank two
configurations at matched cache pressure, and ranking is all an A/B needs.

## Profiles: where every number lives

**Workload profile** (`profiles/*.toml`, committed, one per *use case*). The shape, the
SLO, and which check families apply. `agentic-multiturn` and `single-turn` are opposites
on purpose: the first is dominated by a growing shared prefix, the second has no reuse at
all, and the capacity checks that are load-bearing for one are noise for the other. A
profile that declares its applicable checks is what stops a finding from one shape being
enforced on another.

**Deployment profile** (`profiles/deployments/*.json`, measured, one per model × topology
× build). Produced only by `hgrbench.calibrate`, from runs that actually happened:

- **capacity** per role — read from the engine's own cache config, free, no benchmark;
- **healthy prefix hit band** — a property of a shape on a fleet, not a constant;
- **noise floor** — from repeats, recorded with `n` and with whether they crossed a
  reload; if they did not, it is stored as a lower bound and says so;
- **standard cell** — the smallest run whose answer matches the big one;
- **fingerprint** — image, generation, rendered args. When the live fleet differs, the
  profile is stale and the gate refuses rather than judging against a fleet that is gone.

A calibrated constant beats a written one because a written one goes stale in silence.
The capacity figure this project carried for a month belonged to a build three
generations old; every judgement drawn against it was drawn against a fleet that no
longer existed.

## The gates

`pre` guards spending. `post` judges a finished run from its artifacts. `compare` guards
*claiming*, and it is the one with teeth — a gate that only guards spending gets skipped
at 3am by anyone in a hurry, whereas the moment that genuinely needs guarding is when two
numbers become a sentence in a document. `compare` withholds a difference rather than
qualifying it: no verdicts, no matched shapes, or no measured floor, and it prints
nothing to quote.

Outcomes are four, not three. **`unknown` is not a pass.** The most common way a gate
lies is by skipping a check it could not run and reporting the silence as agreement — one
earlier version passed a 100-request run at concurrency 128 with 35 green rows, because
every check that would have caught it keyed off a flag that run did not set.

Validity and target are also separate verdicts. A configuration can measure exactly what
it claims and still miss its SLO; that is a finding, not a defect in the measurement.

## Windowing: the dump is not the run

Pin the analysis window to the run, never infer it from the file. Scrape files carry
wall-clock time in their mtimes and the load generator records `start_time`/`end_time`,
so the intersection is exact. Falling back — matching a segment's completion count
against the run's request count, and finally the longest active segment — is labelled in
the result, and a number from a "longest" window is a number about an unidentified
benchmark.

Two arithmetic rules that cost real conclusions here: take deltas **per pod and then
sum** (differencing a summed counter goes negative whenever one scrape fails, and taking
one pod's delta as the role's under-reports a two-pod role by half), and compare arms on
**absolute elapsed time**, never on each run's own fraction, because a slower run's
second half sits at a different point in the workload.

Keep per-pod figures in the summary. A role total hides an imbalance completely: one run
here spent its second half with a single prefill worker holding a queue of fifty while
its peer idled, and every role-level number looked like a cache problem.

## Blocks

Attribute a run to one constraint before choosing the next experiment. Thresholds come
from the deployment profile; only the shape of the table is general.

| block | metric | binding when |
|---|---|---|
| admission | running vs the sequence cap, queue time | running is pinned *at* the cap — then the cap is the fix, not more nodes |
| prefix cache | hit rate, working set vs measured capacity | hit rate falls during the run |
| prefill compute | batch fill against the token budget | budget full and the queue non-zero |
| transport | KV transfer time vs the same fleet's healthy figure | orders of magnitude, not a fixed threshold — a working fabric here measures 8–10 ms and any absolute cut-off near it flags a healthy fleet |
| decode block capacity | blocks per request vs pool, preemption cascades | preemptions cascade with the token budget unfilled — the fix is usually the watermark, not the batch size |
| decode interference | per-user output rate vs a disaggregated reference | latency 2–3x worse while cache, queue and preemptions are all clean |
| load balance | least-loaded worker's share of the busiest | one worker queues while its peer idles |
| SLO | the profile's targets | throughput is fine and the target is missed anyway |

The next experiment comes from whichever block is binding, not from the list of untried
flags. Sixty-three ladder rows on one model were measured against batch budget and
sequence caps while the constraint was cache capacity the whole time.

## Order of work

1. **Calibrate.** No profile, no judgements. It is mostly free — capacity is a read.
2. **Baseline across a reload,** A/B/A. Repeats taken back to back on one generation
   give a floor that is too small, and a floor that is too small turns noise into
   findings. The same rendered configuration differed 28% across two generations here for
   a reason still unisolated, which is the argument for a control arm on every generation.
3. **Everything that needs no reload,** against the fleet you already have.
4. **Then the reload-bound queue,** ordered by what buys the most reloads back.
5. **Write down the nulls and the voids.** Most of what is known about the model above
   came from negative results, and none of it came from a run that "worked".

## Stop rules

- Three consecutive void or null results: check the instrument, not the config.
- A question still open after two runs: wrong tier. Drop to 0 or 1.
- Any number that surprises you: re-derive it on a second instrument before building on
  it. The two instruments here now corroborate — telemetry reads 6.83 req/s where the
  client reads 6.71 — and the previous version of that comparison was 1.72 against 6.71,
  which nobody chased.

## What transfers, and what does not

The **failure catalogue** transfers completely and is the real asset: under-firing,
session ceiling, mixed generation, leaked sampler, wrong analysis window, per-pod
aggregation, unmatched shape between arms, a flag that was already set, transport
silently degraded, overlapping runs, load imbalance within a role.

These do **not** transfer and belong in a profile:

- Prefix-cache dominance is a property of the agentic multi-turn shape, not of a model.
- KV replicated across tensor-parallel ranks is a fact about MLA attention (Kimi and
  DeepSeek families) and false for GQA models, where TP does shard KV. Reading capacity
  from the engine makes this irrelevant to the tooling — which is exactly why it is read.
- Disaggregated prefill/decode exists only in some deployments; role detection is by
  behaviour, so an aggregated fleet reads correctly even when its pods are named as
  though it were not.
