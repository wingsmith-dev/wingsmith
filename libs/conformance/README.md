# conformance — what an endpoint does, and whether that matches what it claims to be

`hgrconform` points a set of probes at any OpenAI-compatible endpoint and records what
it observed. With a second endpoint it diffs the two and reports where they disagree.
It knows nothing about Hangar's control plane: a Hangar gateway is one endpoint it can
check, and a model provider's own API is another.

```sh
just conformance <profile>                                   # observe one endpoint
just conformance <profile> --reference moonshot-kimi-k3      # and diff it against the provider
just conformance-probes                                      # what it knows how to ask
just conformance-diff                                        # re-diff the latest stored run
```

Outside the repository it installs as a normal package (`pip install ./libs/conformance`)
and runs as `hgrconform run <profile>`; the committed profiles and video clips ship
inside it.

It exists because an endpoint that serves somebody else's model is usually held to one
sentence: **"consistent with the original model provider."** Not *correct*: consistent.
Whether thinking is on by default, where `usage` sits in a stream, whether
`temperature=1.1` is a 400 or a 200 — nobody says what the answer should be, only that
it should be the provider's answer.

That changes what a test is. There is no expectation to assert against, so a probe
here does not assert. It observes, and the verdict comes from a diff.

## The rule that makes it work

**A probe returns facts, never a verdict.**

The alternative, writing assertions against known-good values, is what most vendor
verifiers do, and it is why each of them works for exactly one model. A suite that
asserts `temperature=1.1` must be rejected is right for a model whose provider pins
that parameter and wrong for every other. Point *this* at another model and you write a
profile.

Four rules follow from it:

- **Vendor knowledge lives in the profile, never in a probe.** A probe containing
  `if vendor ==` is a bug. Thinking syntax, the cached-token field path, the claimed
  context window and modalities are all profile fields. Same pattern as the
  per-harness field maps in [`hgrbench/metrics.py`](../benchmarking/hgrbench/metrics.py),
  and for the same reason.
- **Every exchange is stored verbatim.** A finding that cannot be traced to bytes is
  one nobody can defend.
- **A probe sends no sampling parameter unless the probe is about sampling.** Some
  providers pin them: `temperature` only `1`, `top_p` only `0.95`, penalties only `0`.
  A probe that sends `temperature=0` for determinism it does not need gets a 400 from
  such an endpoint and observes nothing. Prompt token counts and cache residency are
  functions of the prompt alone, so the probes that ask about them send no sampling
  parameter at all. A default chosen for a probe's own convenience is still a vendor
  assumption, and it fails the same way.
- **Raw HTTP, no SDK.** Half of what is asked here is wire shape. `json.loads` folds
  a repeated key into one, which makes "the response must not contain duplicate
  fields" unobservable through any ordinary parse; an SDK does worse.

## The hard part: what may be compared

A comparable key must collapse variance that two honest endpoints would differ on.
This is most of the design work in every probe, and getting it wrong makes the tool
useless in a way that looks like it is working.

`streaming.usage_location` is the worked example. The naive version records which
chunk index carried `usage`, and then every diff reports a difference, because the
two endpoints emitted a different number of tokens. It instead collapses to the three
answers that actually differ for a client:

```
absent | final-chunk | every-chunk
```

So: never a token count, a latency, a generated string, an id, a raw rate or a chunk
index in `compare`. The shape of the answer goes in `compare`; the raw values stay in
`observed` as evidence.

The same goes for the model's own decisions. Whether it called a tool when the choice
was left to it, how many it called, whether it named the colour of an image or the
direction a square moved in a clip, whether it used a tool result: each is one sample
of the model's judgement, and two honest runs of one model disagree on a single sample
often enough that comparing it reports noise. Those readings are kept as evidence for
a person (`hgrconform show <run> <probe>`) and never compared. Rates are compared only
as buckets, over enough samples that every bucket can be reached (`eos.suppression`).

The deliberate exceptions are `tokens.prompt_accounting` and the prompt counts in the
`video` probes, which compare reported prompt token counts directly. Two endpoints
serving one model must tokenize identically, and must turn one committed clip into
the same number of tokens, so there the number *is* the answer.

## Keeping that true

`tests/stub.py` is a small OpenAI-compatible server that can be told to misbehave. It
serves every surface the probes ask about: image, audio and video parts, a thinking
switch, streamed tool calls and the Anthropic Messages surface. Its `noisy` mode is
conforming but varies token counts, ids and chunk counts on every call, and makes the
model's decisions differently from its twin. Two noisy instances are two honest
endpoints, and

```sh
just conformance-test
```

runs every probe, the heavy ones included, against both and fails if anything is
reported, if any probe raises, or if any is skipped. It also runs the probes against a
`leaky` stub with planted defects and checks that each one is found. That is a
regression test on the rules above, and it is the check to run after writing a probe.

## What is here, and what is deliberately not

| Probes | What they ask |
|---|---|
| `auth`, `basics`, `errors`, `protocol` | authentication, response correctness, error envelopes, the three API surfaces |
| `streaming`, `length`, `sampling` | streaming and usage location, output caps and reasoning-only exhaustion, parameter acceptance |
| `structured`, `tools` | `response_format` in three modes, tool-call protocol, round trip, restraint, tool call id uniqueness across turns on both the chat and Messages surfaces |
| `thinking`, `cache`, `multimodal`, `tokens` | thinking mode, cached tokens, image input, token accounting |
| `video` | video input with the clips in `hgrconform/assets/video/`: acceptance, image and video order, token cost and frame sampling, count, duration and size limits, streaming |
| `eos`, `limits` | EOS suppression rate, burst policy |

Not here, on purpose:

- **Accuracy benchmarks** (AIME, MMLU_Pro, SWE-Bench, GPQA…). inspect-ai and EvalScope
  run these. Reimplementing them would be worse and would date faster.
- **Performance and capacity** (TTFT buckets, output speed tiers, TPM). That is
  [`libs/benchmarking`](../benchmarking/). A conformance run must never be capable of
  degrading a live endpoint, which is why `limits.burst` observes the *shape* of a
  rate-limit policy and never searches for its ceiling.
- **Availability, RTO, blocking policy.** Not tests. Monitoring, an incident drill,
  and a sentence somebody writes.
- **An acceptance ledger** — a document saying which requirement is answered by which
  of the above. It spans all of them, and the probes are useful without it.

## Profiles

A profile is committed TOML naming the endpoint and the handful of things no probe is
allowed to know. Three ship in `hgrconform/profiles/`:

- `example` — the annotated template. Copy it per model.
- `gateway` — a Hangar gateway's public listener on `127.0.0.1:8080`, with a tenant API
  key in `HANGAR_API_KEY`.
- `moonshot-kimi-k3` — an example reference profile: a model from its original
  provider, to diff other endpoints serving that model against.

A profile names the variable holding its bearer token in `token_env`
(`HANGAR_API_KEY` when it names none); the token itself is never written to the run
directory. `run` and `--reference` also take a path to a profile of your own.

## Probes that need no reference

A probe declaring `compare=()` is asking a question the reference endpoint cannot
answer. `errors.leakage` is the case: it checks whether an error body names the
infrastructure serving the request, and the original provider will never leak your
hostname. Those are reported separately rather than diffed, and never silently dropped.

## Running it unattended

`hgrconform` is meant to be run by hand today. The exit code already works — `diff`
returns 1 on any difference — but wiring "any difference fails" to a schedule or a
deploy gate goes red on the first run, because every endpoint differs from its
reference on something benign. What that needs, and does not have yet, is an accepted
run stored per target, an alarm on differences that are *new* against it, and
accepting a difference as an explicit act that leaves a record. Runs are already
written timestamped per target under the results directory, and `hgrconform diff`
re-diffs a stored run without touching the endpoint.

Two more things to know before trusting a diff:

- **A brokered reference can flap.** A broker that serves one model id from many
  upstreams picks per request, so an unpinned reference is not the original provider.
  Pin the upstream with `extra_body` in the reference profile, and treat a difference
  seen once as something to confirm with a second run.
- **Some questions have an answer without a reference.** Tool call ids must be unique
  for as long as a conversation lives; an id built from the tool name and a counter
  that resets every response cannot be, and knowing that needs no reference at all.
  `tools.id_uniqueness` records it as an observation and diffs it like everything
  else. Whether this design should grow a place for invariants checked on their own is
  an open question.

## Writing one

```python
@probe(
    id="group.thing",
    title="What it asks, in one line",
    requirement="What it checks, in plain words",
    compare=("shape_of_the_answer",),
)
def thing(session: Session) -> dict[str, Any]:
    response = session.post("/v1/chat/completions", session.body(max_tokens=32))
    return {"shape_of_the_answer": ..., "raw_value_as_evidence": ...}
```

Drop the module in `hgrconform/probes/` — the registry discovers it, there is no list to
edit. Raise `Skip("reason")` when the profile means the question does not apply; two
skips compare as agreement. Teach `tests/stub.py` enough to answer it, then run
`just conformance-test`.
