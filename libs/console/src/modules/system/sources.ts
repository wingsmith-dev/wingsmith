import type { MetricsSourceConfig } from "./types"

/** One row of the metrics-sources table: a cluster, or a source with no cluster. */
export interface SourceRow {
  /** The cluster's name, or the entry's key when no cluster answers to it. */
  key: string
  source: MetricsSourceConfig | null
  /** False for an entry whose key matches no cluster in the fleet. */
  known: boolean
}

/** The minimum of a cluster this join needs. Source keys match a name or a uid. */
export interface SourceCluster {
  id: string
  name: string
  /** The `cluster` label narrowing the shared store to this cluster, if it has one. */
  metrics_label?: string | null
}

/**
 * Joins every cluster to the source that answers for it, then appends any source
 * entry that matched no cluster.
 *
 * Both halves are the point, and neither is visible from a source list alone. A
 * cluster with no entry is the failure that hides, its panels come back empty
 * and nothing errors, and an entry matching no cluster is usually the typo that
 * caused it. Clusters come first and in their own order so the table reads as
 * the fleet with gaps in it, not as configuration with extras.
 *
 * A cluster is answered two ways and only the first is in `sources`, which is
 * read from the environment: an explicit entry wins, and otherwise the shared
 * store narrowed by the cluster's own `metrics_label`. Counting only the entries
 * would report every normally-configured cluster as unmapped, so the second form
 * is synthesized here, from the same two facts the plane resolves it from.
 */
export function sourceRows(
  sources: MetricsSourceConfig[],
  clusters: SourceCluster[],
  sharedUrl = ""
): SourceRow[] {
  const matched = new Set<string>()
  const rows: SourceRow[] = clusters.map((cluster) => {
    const entry =
      sources.find(
        (s) => s.cluster === cluster.name || s.cluster === cluster.id
      ) ?? null
    if (entry) matched.add(entry.cluster)
    const source =
      entry ??
      (cluster.metrics_label && sharedUrl
        ? {
            cluster: cluster.name,
            url: sharedUrl,
            kind: "prometheus",
            label: cluster.metrics_label,
          }
        : null)
    return { key: cluster.name, source, known: true }
  })
  for (const source of sources) {
    if (matched.has(source.cluster)) continue
    rows.push({ key: source.cluster, source, known: false })
  }
  return rows
}

/** How many clusters have no source at all: the number the panel warns on. */
export function unmappedCount(rows: SourceRow[]): number {
  return rows.filter((row) => row.known && !row.source).length
}
