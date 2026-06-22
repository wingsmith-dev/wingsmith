# transit — how far the request path scales, and what stops it

`hgrtransit` measures hangar's data path: **public client → gateway → tunnel → agent → the local
OpenAI-compatible frontend**. It answers two questions in this order — *where is the bottleneck*
and *how much can one gateway replica carry* — and it answers them without a GPU, without
Kubernetes, and without a model.

It is not `hgrbench`. That measures an engine on real hardware. This measures the plumbing, with the
engine replaced by a stopwatch.

## The three environments, and what each is allowed to prove

One binary, one generator, one report shape, so a laptop number, a CI number and a production number
can be put side by side.

| Mode | What is real | Blast radius | Safe to run against production? |
|---|---|---|---|
| `synthetic` | gateway binary, agent binary, tunnel, both reverse proxies | this host only: child processes it started, temporary ports, no network egress | n/a — it touches nothing shared |
| `live` | everything, including a real engine | **offered load against a shared serving path** | only at a concurrency somebody has agreed to; see below |
| `upstream` / `plane` | one fake, on its own | a port | yes, it serves nothing of ours |

**`live` against production.** It is a real caller doing real completions, which cost real GPU
seconds and share a queue with real customers. The default ramp is `1,2,4` and any stage above
`-max-concurrency 8` is refused outright, so the default posture is a probe rather than a load test.
Raising it is a decision, and it should be taken with the same seriousness as pointing a load
generator at any customer path: pick a low-traffic window, watch the gateway's own
`hangar_gateway_*` series while it runs, and stop when the endpoint's latency moves. What is always
safe is the shape of it — one caller, a handful of streams, for twenty seconds — which is enough to
compare production's TTFT distribution against the lab's.

**Numbers that do not compare across environments, and why.**

- *TTFT and inter-token latency.* In synthetic mode they are the fake frontend's schedule plus the
  path; in live mode they are a real engine's prefill and decode, which dominate everything the
  transport does. The comparable quantity is the **overhead over baseline**, which only synthetic
  mode can produce, because only there is the frontend's own contribution known.
- *Throughput and tokens/minute.* Synthetic mode has no GPU and will happily carry a concurrency no
  cluster could serve. Its throughput number is a ceiling for the transport, never a capacity for
  the product.
- *Per-leg and per-process columns.* They need a shared clock and child processes, so they are
  present in synthetic mode and empty in live mode. Empty, not estimated.
- *Latency floor.* Loopback is roughly 0.05 ms and a WAN is 30–80 ms. Every synthetic figure is
  missing one round trip's worth of everything.

## What it measures, and how the attribution works

Per stage, per request: TTFT, every inter-token gap, total duration, and the failure bucket.
Aggregated to p50/p90/p99, inter-token jitter, request rate, token rate, and success rate.

Attribution comes from four independent signals, so the named bottleneck is evidence rather than a
guess:

1. **Per-leg split.** The fake frontend stamps the wall clock when it received a request
   (`X-Transit-Received-Ns`) and when it wrote each token (`transit_write_ns` in the chunk). One
   host means one clock, so TTFT decomposes into the inbound leg — gateway parse, routing, tunnel,
   agent, local proxy — and the return leg. This needed no change to the gateway or the agent.
2. **Per-process cost.** CPU and RSS of the gateway and each agent, sampled every second, reported
   as mean and peak per stage.
3. **Where requests were held.** The frontend's own peak in-flight count against the concurrency
   offered. Fewer means something between the caller and the frontend held them — stream limits,
   flow control, or a proxy — and it says so.
4. **Baseline subtraction.** The first stage is also run straight at the frontend, so the whole
   path's cost is a subtraction rather than an assertion.

The one thing this cannot separate is **gateway-internal time from tunnel time from agent time on
the inbound leg**. Splitting those needs a stamp inside the gateway and the agent. The
non-invasive way to get it is a `Server-Timing` response header written by each hop — additive, off
by default, and readable by anything. That is a proposal, not something this harness did silently.

## Running it

```sh
just transit                       # the default ramp, 8/32/128
just transit "4,16,64,256,512"     # a ramp of your own
just transit-smoke                 # the CI shape: short, no baseline
just transit-live https://gateway.example.com my-model
```

Useful flags on `synthetic`:

| Flag | Asks |
|---|---|
| `-streams 16 -conns 1` | what happens at the per-tunnel stream limit |
| `-agents 3 -conns 2` | many tunnels to one gateway against one carrying everything |
| `-kill-agent-every 3s` | what a caller sees when an agent dies mid-stream, with no GOAWAY |
| `-ttft 2s -itl 50ms -tokens 512` | a long generation, where the stream is held open for minutes |
| `-stream=false` | the non-streaming path |
| `-slo-ttft-p99`, `-slo-success` | the condition the capacity number is reported under |

## The questions, ranked

1. **Concurrent streams per tunnel before anything degrades**, and what the far side of the limit
   looks like. This is first because the limit is admission, not queueing: past it the pool answers
   `ErrSaturated` and the gateway sheds with a 503.
2. **Cost of shedding.** A rejection is far cheaper than a completion, so a saturated cluster can
   cost more CPU than a served one. Measured, and it is real — see below.
3. **How much one gateway replica carries** at a stated success rate and TTFT band, and what
   saturates next.
4. **Many agents against one carrying everything.** Coverage is per-replica and the pool is
   least-loaded, so this is a question about the pick, not about bandwidth.
5. **An agent dying mid-stream.** Requests whose headers have already gone out cannot be retried by
   design; the number worth knowing is how many that is per kill.
6. **Head-of-line blocking on one HTTP/2 connection**, visible as inter-token jitter rather than as
   mean latency.
7. **Memory per in-flight request**, from RSS against concurrency.
8. **Reconnect storms.** Every agent redials when the gateway rolls; the fake plane counts the token
   verifications that produces.

## Limits of what this can prove

- **It is not a network.** No WAN latency, no packet loss, no MTU, no TLS on the tunnel by default,
  and every process competes for one laptop's cores. Absolute throughput here is optimistic and the
  latency floor is fictional.
- **It is not a model.** The frontend never runs out of KV cache, never queues, and never gets
  slower under load. Everything a real cluster does when it is full is absent.
- **It is one gateway replica.** Multi-replica coverage, the Service in front of it, and the
  load balancer's choice of replica are not exercised.
- **The control plane is a fixture.** It proves nothing about the plane, the table the plane really
  derives, or what happens when the plane is slow.
- **CPU is sampled, not accumulated.** A one-second poll can miss a spike; the peak is printed
  beside the mean for that reason.
