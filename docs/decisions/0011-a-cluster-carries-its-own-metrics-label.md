# 0011 — A cluster carries its own metrics label

**Status:** accepted · 2026-08-27

**Amends:** [0008](0008-metrics-are-answered-by-a-backend-not-by-promql.md), first consequence, and
through it [0003](0003-telemetry-is-read-beside-the-api-not-through-the-hub.md) point 2. Everything
those decided about *what* answers a metric is unchanged; this moves *where the mapping lives*.

## Context

0003 kept the source mapping in an environment variable and gave two reasons. The shape was still
moving, so a column would be migrated every time it moved. And `Cluster` holds that the plane never
dials a cluster, so a URL had no business on that table.

0008 changed what the mapping is. There is one store, it stamps `cluster` on every series, and an
entry that carries no URL of its own contributes nothing but a label — defaulting to the entry's own
key. What was left in the variable per cluster was therefore a cluster name written twice: once as
the key, once as the label it defaults to. Both reasons 0003 gave had expired with it. The shape had
stopped moving, and a label is not an address.

The cost of leaving it there is that enrolling a cluster needed a config edit and a restart of the
plane, in a step that appears nowhere in the register flow the console walks an operator through.
When that step is skipped, the cluster's panels error while everything else about it reads as
enrolled and healthy. A cluster's own facts had to be declared in the one place declaring a cluster
does not reach.

The label is also not a guess, which is the part that makes this safe.
[`libs/chart/cluster-setup`](../../libs/chart/cluster-setup/) writes the metrics agent's
`external_labels.cluster` from `.Values.cluster.id`, and the plane renders that value into the
install instructions as the cluster's name. The label a cluster's series carry and the name the plane
knows it by are the same string because one is derived from the other.

## Decision

1. **`Cluster.metrics_label` is declared state on the registry row.** It is the `cluster` label value
   this cluster's series are stamped with in the shared store — not an endpoint, not a credential,
   and not something the plane could dial. It is set at registration to the cluster's own name, for
   the reason above: that is the identity the chart is installed with, so asking for it a second time
   would only create a second thing that can disagree.
2. **Null still means no source, and says so.** This is 0008's second consequence kept exactly, moved
   from "has no entry" to "has no label". A cluster with a null label is not queried under a guessed
   one, because a store that holds nothing under that label answers a clean empty and a clean empty
   reads as a healthy idle fleet.
3. **`HANGAR_METRICS_SOURCES` keeps the endpoint and the credential.** Those are genuinely
   process-level: one store fronts the fleet, the token is a deployment secret, and neither is a fact
   about any cluster. They do not move to the database.
4. **An explicit env entry still wins over the column.** `<cluster>|label=<name>` overrides the label
   for one cluster and `<cluster>=<url>` still names a store holding that cluster alone, queried with
   no cluster matcher at all. This keeps 0008's decommissioning path open and, more importantly, means
   a label the column has wrong is fixable from either side — the database without a redeploy, or the
   deployment without a database write.
5. **[`sources.py`](../../libs/plane/app/modules/metrics/sources.py) stays pure.** `resolve` takes an
   already-loaded `Cluster` rather than a session. The module is a function of its arguments and the
   environment, and the one caller already had the row.

## Consequences

- Enrolling a cluster no longer touches the deployment. Register, install the chart, and the label is
  already the one the agent will stamp.
- The trade this accepts is that a registered cluster which never turns on `monitoring.enabled` now
  renders em-dashes instead of "no metrics source". That is a weaker signal than 0008 had and it is
  the price of the failure above; it is bounded by 0003 point 6 — a missing series is null end to end
  and draws as `—`, never as 0% — and the label can be cleared to get the explicit message back.
- Existing rows were **not** backfilled with their own names. A cluster can be left out of the
  variable on purpose — because its series stopped landing — and a blanket backfill would convert that
  judgement into an empty chart. Only clusters the deployed value already listed were carried over.
  Opting one in is an `UPDATE` or a `PATCH`.
- `GET /v1/metrics/sources` now walks the registry rather than parsing the variable, because that is
  where most of the answer lives. Env entries matching no cluster are still listed, so a typo in one
  is visible rather than silently inert.
