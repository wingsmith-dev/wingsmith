# ADR 0008: Metrics are answered by a backend, not by PromQL

**Status:** accepted · 2026-08-19 · first consequence amended by ADR 0011

**Amends:** [0003](0003-telemetry-is-read-beside-the-api-not-through-the-hub.md) (points 1 and 4)

## Context

ADR 0003 settled where the console reads telemetry from, and two of its five points rested on
assumptions about where metrics live that no longer hold.

It said there was no shared store: no cluster remote-wrote anywhere and none set `externalLabels`,
so the only accurate mapping was one Prometheus per cluster, and a query with no cluster in it could
not be wrong because there was nothing else in the store to blend with. The expected setup is now
the opposite: clusters remote-write into one central Prometheus which stamps `cluster` and
`region` on every series, and such a store routinely holds clusters that are not inference
clusters at all, database clusters among them. An unscoped query stopped being harmless and became
a number that adds inference fleets and database fleets together and looks entirely plausible.

The second premise was quieter. ADR 0003 said metric names are Wingsmith's and dialects are a table,
which was the right idea implemented one layer too shallow: a `Metric` carried the PromQL that
answered it, so the vocabulary clients name was a Prometheus vocabulary. Three consequences showed
up at once when the queries were finally run against a live store, not written from the
manifests:

- The node scope label was `Hostname`. The deployed exporter emitted lowercase `hostname`, so every
  node-scoped and pool-scoped panel had returned zero series since the day it was written.
- `vllm:time_per_output_token_seconds` was renamed upstream to `vllm:inter_token_latency_seconds`,
  and `vllm:gpu_cache_usage_perc` to `vllm:kv_cache_usage_perc`. One expression per metric means an
  upstream rename is indistinguishable from an idle cluster.
- Two DCGM fields the registry named were not in the deployed exporter's field csv at all.

None of these fail. They all render as an empty panel, which is why they survived.

## Decision

1. **`registry.py` is vendor-neutral.** A `Metric` is a Wingsmith name, a unit, a family, a title and
   how it aggregates. It names no series, no label and no query language. This is the API's
   vocabulary and clients depend on it, so it must not move because an engine renamed something.
2. **The dialect is a per-backend table keyed by the Wingsmith name.** `promql.py` holds the series
   names, the label names and the expression shapes. A second store is a second table.
3. **A `Backend` answers metrics for a scope over a window.** Not "run this PromQL": that is a
   transport seam, and `client.py` already was one. The protocol is `answers`, `samples`,
   `breakdown` and `curves` over a `Scope` expressed in Wingsmith's dimensions, which is what makes a
   ClickHouse or OTel implementation a new file instead of a refactor, and what leaves room for a
   backend that answers an hourly cost per node out of inventory and never scrapes anything.
   `Source.kind` selects it; it has been the string `prometheus` since the module was written and
   was decorative until now. All of this lives under
   [`libs/plane/app/modules/metrics/`](../../libs/plane/app/modules/metrics/).
4. **Every query carries a cluster matcher.** A `Scope` holds the cluster separately from its other
   dimensions because it is the one that is never optional. The exception is narrow and explicit: a
   source configured with its own endpoint and no label is a store holding one cluster, which has no
   such label to match on.
5. **A metric names candidate series in preference order, not one series.** They render into a
   PromQL `or` chain, which gives first-non-empty fallback and a clean empty when none of them
   exist. Renamed series keep both spellings; fields the exporter does not emit yet stay listed and
   begin working when its csv grows, with no code change.
6. **A summed metric never unions its candidates.** `or` is a set union, so two candidates populated
   at once (a real upgrade window where an engine emits the old and the new name) would add the
   same tokens twice under a `sum`. Summed metrics with more than one candidate probe in order and
   use the first that has any series. A silently doubled token rate is the worst failure available
   to this module, and it is worth one extra query to make it unreachable.

Points 2, 3, 5 and 6 of ADR 0003 stand unchanged: the plane queries and never the browser,
measurement never joins an inventory response, `MetricsUnavailable` is a 503 with a reason, and
absent is never zero.

## Consequences

- `WINGSMITH_METRICS_SOURCES` grows a `default=` endpoint and a per-entry `label=`. The old
  `cluster=url` form still parses and still means a dedicated store with no cluster matcher, so
  per-cluster Prometheus instances keep working; moving one onto the shared store is adding
  `label=` to its entry.
- A cluster with no entry still has no source and still says so. It is intentionally not pointed
  at the shared store by default, because a guessed label there returns a clean empty that reads as a
  healthy idle fleet: the failure mode this whole module is arranged against.
- Cross-cluster rollups are now possible in one query instead of by client-side fan-out. Nothing
  uses that yet.
- The dialect table is the thing to check against a live store when a panel is empty, and it should
  be checked against a live store, which is how all of the above was found.

> **Amended.** The per-cluster half of `WINGSMITH_METRICS_SOURCES` carried nothing but a label, and
> declaring it there meant enrolling a cluster required a ConfigMap edit that the register flow never
> mentions. It is now `Cluster.metrics_label`, set at registration to the name the chart stamps its
> series with. The variable keeps the `default=` endpoint and its credential, an explicit entry still
> overrides the column, and the second consequence above is unchanged: a cluster with no label still
> has no source and still says so. See ADR 0011.
