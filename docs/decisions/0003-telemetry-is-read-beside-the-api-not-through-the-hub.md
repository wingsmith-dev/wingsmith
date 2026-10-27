# ADR 0003: Telemetry is read beside the API, not carried through the hub

**Status:** accepted · 2026-08-19 · points 1 and 4 amended by ADR 0008

## Context

The console could answer what hardware exists and what is declared onto it, and nothing about how
any of it is behaving. Every operator question that motivated Hangar, is this cluster busy, is a
card running hot enough to start an RMA, what is this deployment's time to first token, needed a
number the API did not have.

`libs/packages/contract/API.md` §5 excludes telemetry: utilization, temperature,
throughput, latency and token counts do not traverse the hub, and `MESSAGES.md` puts it as capacity
being state and occupancy being measurement. That exclusion is right and is not being revisited
here: measurement has a sampling rate three orders of magnitude off the rate at which inventory
changes, and pushing it through the sync channel would make the channel about metrics.

The open question was therefore not whether to carry telemetry, but where the console reads it from.

Two facts about the clusters Hangar was first run against settled the shape. They already ran
kube-prometheus-stack with DCGM-exporter and a PodMonitor for the serving engine, so the numbers
already existed and were already scraped. And each cluster stored its own metrics: there was no
`remote_write` to a central store, and no cluster set `externalLabels`. There was no single store
to query with a `cluster="..."` selector.

## Decision

Telemetry is read from the metrics stores directly, by the plane, at request time.

1. **One source per cluster**, configured as
   `HANGAR_METRICS_SOURCES="us=https://…,eu=https://…"`. Not a label selector against a shared
   store, because there is no shared store, and blending two fleets into one number is worse than
   not answering.
2. **Configuration is an environment variable, not a column.** The shape is still moving; a column
   would be migrated every time it moves. `Cluster` also holds that the plane never dials a cluster:
   its identity is the credential its agent presents. A metrics store is a different endpoint and
   dialling it breaks no invariant, but it does not belong on that table either.
3. **The plane queries, never the browser.** In-cluster Prometheus is not reachable from a console
   tab, and the query vocabulary, credentials and read-only posture all belong in one place.
4. **Metric names are Hangar's; dialects are a table.**
   [`libs/plane/app/modules/metrics/registry.py`](../../libs/plane/app/modules/metrics/registry.py)
   is the single place that knows DCGM spells it `DCGM_FI_DEV_GPU_UTIL` and vLLM spells its own
   `vllm:time_to_first_token_seconds`. Adding SGLang or TensorRT-LLM is a new entry in `ENGINES`.
5. **Measurement never joins an inventory response.** `/v1/metrics/...` is its own surface, so a
   Prometheus that is down cannot make a node page fail. `MetricsUnavailable` is 503 with a reason
   the console renders, because the inventory answer is still perfectly good.
6. **Absent is not zero.** A sample value is nullable end to end and renders as `—`. A missing
   series drawn as 0% reads as an idle GPU, which is the one wrong answer that looks right.

> **Amended.** Central `remote_write` is now the expected setup: clusters write into one store
> that stamps `cluster` on every series, so point 1's premise is gone. Every query now carries a
> cluster matcher, and per-cluster endpoints remain as an override for a store that holds one
> cluster. Point 4 was right about names and one layer too shallow about dialects: the expressions
> have moved out of the metric and behind a `Backend`. See ADR 0008.

## Consequences

- Cross-cluster rollups are client-side fan-out until a shared store exists. Acceptable at a
  handful of clusters; the fix is upstream, not here.
- Scoping joins differ in strength, and the weak one is named in the code: node is the exporter's
  host label, device is the GPU's own `UUID`, and a deployment is selected by `model_name`, so two
  deployments of one model in one cluster read as one. `served_model_name` in the engine args is
  the operator's way to disambiguate.
- The plane now makes outbound HTTP at request time. It is `urllib` in a worker thread instead of
  a new dependency, so the seam is one file if that ever needs to be httpx.
