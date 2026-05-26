# conformance — what an endpoint does, and whether that matches what it claims to be

`hgrconform` points a set of probes at an OpenAI-compatible endpoint and records what
it observed. With a second endpoint it diffs the two and reports where they disagree.

```sh
just conformance <profile>                          # observe one endpoint
just conformance <profile> --reference moonshot     # and diff it against the real one
just conformance-probes                             # what it knows how to ask
```

It exists because of one sentence, which appears in almost every row of the model
acceptance standards buyers send us: **"must be consistent with the original model
provider."** Not *correct* — consistent. Whether thinking is on by default, where
`usage` sits in a stream, whether `temperature=1.1` is a 400 or a 200: the standard
does not say what the answer should be. It says our answer must be the provider's
answer.

That changes what a test is. There is no expectation to assert against, so a probe
here does not assert. It observes, and the verdict comes from a diff.

## The rule that makes it work

**A probe returns facts, never a verdict.**

The alternative — writing assertions against known-good values — is what the vendor
verifiers in the wild do, and it is why each of them works for exactly one model.
Kimi's suite asserts that `temperature=1.1` must be rejected, because Kimi pins that
parameter. Point it at another model and every assertion is wrong. Point *this* at
another model and you write a profile.

Four rules follow from it:

- **Vendor knowledge lives in the profile, never in a probe.** A probe containing
  `if vendor ==` is a bug. Thinking syntax, the cached-token field path, the claimed
  context window and modalities are all profile fields. Same pattern as the
  per-harness field maps in [`hgrbench/metrics.py`](../benchmarking/hgrbench/metrics.py),
  and for the same reason.
- **Every exchange is stored verbatim.** A finding that cannot be traced to bytes is
  one nobody can defend in front of the person whose acceptance document it is.
- **A probe sends no sampling parameter unless the probe is about sampling.** Found
  2026-09-01 against Moonshot, which pins K3: `temperature` may only be `1`, `top_p`
  only `0.95`, both penalties only `0`, `logprobs` only `false`. `tokens` and `cache`
  were each passing `temperature=0` for determinism they never needed — prompt token
  counts and cache residency are both functions of the prompt alone — so on a pinned
  endpoint all three probes returned 400 and observed nothing. A default chosen for a
  probe's own convenience is still a vendor assumption, and it fails the same way.
- **Raw HTTP, no SDK.** Half of what is asked here is wire shape. `json.loads` folds
  a repeated key into one, which makes "the response must not contain duplicate
  fields" unobservable through any ordinary parse; an SDK does worse.

## The hard part: what may be compared

A comparable key must collapse variance that two honest endpoints would differ on.
This is most of the design work in every probe, and getting it wrong makes the tool
useless in a way that looks like it is working.

`streaming.usage_location` is the worked example. The naive version records which
chunk index carried `usage` — and then every diff reports a difference, because the
two endpoints emitted a different number of tokens. It instead collapses to the three
answers the standard actually distinguishes:

```
absent | final-chunk | every-chunk
```

So: never a token count, a latency, a generated string, an id, a raw rate or a chunk
index in `compare`. The shape of the answer goes in `compare`; the raw values stay in
`observed` as evidence.

The one deliberate exception is `tokens.prompt_accounting`, which compares reported
prompt token counts directly. Two endpoints serving one model must tokenize
identically, so there the number *is* the answer.

## Keeping that true

`tests/stub.py` is a small OpenAI-compatible server that can be told to misbehave.
Its `noisy` mode is conforming but varies token counts, ids and chunk counts on every
call. Two noisy instances are two honest endpoints, and

```sh
just conformance-test
```

runs the whole probe set against both and fails if anything is reported. That is a
regression test on the rule above, and it is the check to run after writing a probe.

## What is here, and what is deliberately not

| Probes | Rows of the standard |
|---|---|
| `auth`, `basics`, `errors`, `protocol` | authentication, response correctness, error envelopes, the three API surfaces |
| `streaming`, `length`, `sampling` | streaming and usage location, output caps and reasoning-only exhaustion, parameter acceptance |
| `structured`, `tools` | `response_format` in three modes, tool-call protocol, round trip, restraint, tool call id uniqueness across turns on both the chat and Messages surfaces |
| `thinking`, `cache`, `multimodal`, `tokens` | thinking mode, cached tokens, image input, token accounting |
| `eos`, `limits` | EOS suppression rate, burst policy |

Not here, on purpose:

- **Accuracy benchmarks** (AIME, MMLU_Pro, SWE-Bench, GPQA…). inspect-ai and EvalScope
  run these. Reimplementing them would be worse and would date faster.
- **Performance and capacity** (TTFT buckets, OTPS tiers, TPM). That is
  [`libs/benchmarking`](../benchmarking/). A conformance run must never be capable of
  degrading a live endpoint, which is why `limits.burst` observes the *shape* of a
  rate-limit policy and never searches for its ceiling.
- **Availability, RTO, blocking policy.** Not tests. Monitoring, an incident drill,
  and a sentence somebody writes.
- **The acceptance ledger itself** — the document that says which requirement is
  answered by which of the above. It spans all of them, its schema is the part most
  likely to be wrong on the first attempt, and the probes are useful without it.

## Known gap: this detects, it does not guard

Recorded 2026-09-01, found while debugging Kimi K3 on Dynamo. The `kimi_k3` tool parser
mints call ids as `<tool_name>:<index>` with the index resetting on every assistant
response, so `Bash:0` recurs turn after turn and breaks Anthropic `tool_use` /
`tool_result` pairing. `tools.id_uniqueness` and `tools.messages_id_uniqueness` now
observe exactly that. The gap is that nothing would have told anyone.

`hgrconform` is run by hand and only by hand. Nothing invokes it on a schedule, on a
deployment, or before a runtime image takes traffic, so a regression is found when
somebody happens to look. The exit code already works — `diff` returns 1 on any
difference — so what is missing is an invoker, not a verdict.

The naive invoker would be worse than none, and this is the part that matters. There is
no baseline here and no way to say a difference is accepted; every endpoint differs from
its reference on something benign, so wiring "any difference fails" to a cron goes red on
day one for reasons nobody cares about and is ignored by the end of the week — taking any
real signal down with it. What is wanted instead: an accepted run stored per target, an
alarm on differences that are *new* against it, and accepting a difference being an
explicit act that leaves a record. Half the machinery is already here — runs are written
timestamped per target under the results directory, and `hgrconform diff` re-diffs a
stored run without touching the endpoint.

A reference is not automatically trustworthy either. Diffing that same Kimi K3 deployment
against an OpenRouter reference, the reference itself returned *mixed* tool-call id shapes
on `/v1/messages` — `["chatcmpl-tool-9b6a75...", "get_weather:0", "chatcmpl-tool-806091..."]`.
OpenRouter brokers per request, and one of its upstreams runs the same defective parser we
were testing for. A brokered reference can flap, so a difference seen once needs a second
run to confirm it.

Both halves of that were then measured, and the second half was wrong in an instructive way.
Parsing the `provider` field out of every recorded exchange of one unpinned run: Together
answered 136 requests, Phala 63, Sail Research 5, Morph 2, DigitalOcean 1, and Moonshot none
at all — `moonshotai/kimi-k3` is served there by eighteen endpoints across fifteen providers.
So the profile now pins the upstream, and a pinned run is answered by Moonshot 246 times out
of 246. But the `name_index` ids did not go away with the re-hosts: Moonshot mints
`get_weather:0` itself. The shape is the provider's own convention and its counter simply does
not reset, which is the defect — not the shape. Consistency here means minting `name_index`
ids that stay unique, not opaque ones.

That leaves an open question, and it is left open. The rule above is that probes observe
and never assert, and that the verdict is a diff against the original provider. That
serves provider *conventions* well. But tool-call id uniqueness is not a convention, it
is a protocol *invariant*: an id derived from the tool name cannot be unique across turns,
and knowing that needs no reference at all. It is the first case here where the invariant
is more trustworthy than the reference. Whether this design should grow a place for
invariants is for the maintainer to decide — recorded, not answered.

## Probes that need no reference

A probe declaring `compare=()` is asking a question the reference endpoint cannot
answer. `errors.leakage` is the case: it checks whether an error body names our own
infrastructure, and the original provider will never leak our hostname. Those are
reported separately rather than diffed, and never silently dropped.

## Writing one

```python
@probe(
    id="group.thing",
    title="What it asks, in one line",
    requirement="The row of the standard it answers, quoted",
    compare=("shape_of_the_answer",),
)
def thing(session: Session) -> dict[str, Any]:
    response = session.post("/v1/chat/completions", session.body(max_tokens=32))
    return {"shape_of_the_answer": ..., "raw_value_as_evidence": ...}
```

Drop the module in `probes/` — the registry discovers it, there is no list to edit.
Raise `Skip("reason")` when the profile means the question does not apply; two skips
compare as agreement. Then run `just conformance-test`.
