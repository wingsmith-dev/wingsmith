import { describe, expect, it } from "vitest"

import { sourceRows, unmappedCount } from "./sources"
import type { MetricsSourceConfig } from "./types"

const source = (cluster: string, label = ""): MetricsSourceConfig => ({
  cluster,
  url: "https://prom.central:9090",
  kind: "prometheus",
  label,
})

const CLUSTERS = [
  { id: "clu_a", name: "iad-1" },
  { id: "clu_b", name: "sjc-1" },
]

describe("sourceRows", () => {
  it("joins a cluster to the entry keyed by its name", () => {
    const rows = sourceRows([source("iad-1", "iad-1")], CLUSTERS)
    expect(rows[0]).toMatchObject({ key: "iad-1", known: true })
    expect(rows[0].source?.label).toBe("iad-1")
  })

  it("joins a cluster to an entry keyed by its uid", () => {
    const rows = sourceRows([source("clu_b")], CLUSTERS)
    expect(rows[1].source).not.toBeNull()
  })

  it("keeps a cluster with no entry, which is the failure this reveals", () => {
    const rows = sourceRows([source("iad-1")], CLUSTERS)
    expect(rows).toHaveLength(2)
    expect(rows[1]).toEqual({ key: "sjc-1", source: null, known: true })
    expect(unmappedCount(rows)).toBe(1)
  })

  it("appends an entry that matches no cluster, marked unknown", () => {
    const rows = sourceRows([source("iad-1"), source("typo-1")], CLUSTERS)
    expect(rows.map((row) => row.key)).toEqual(["iad-1", "sjc-1", "typo-1"])
    expect(rows[2].known).toBe(false)
  })

  it("reports every cluster unmapped when nothing is configured", () => {
    const rows = sourceRows([], CLUSTERS)
    expect(unmappedCount(rows)).toBe(2)
  })

  it("does not count an unmatched entry as an unmapped cluster", () => {
    expect(unmappedCount(sourceRows([source("typo-1")], []))).toBe(0)
  })
})

describe("sourceRows with a shared store", () => {
  const SHARED = "http://prom.internal:9090"
  const LABELLED = [{ id: "clu_1", name: "iad-1", metrics_label: "iad-1" }]

  it("counts a cluster answered by its own label as mapped", () => {
    const rows = sourceRows([], LABELLED, SHARED)
    expect(unmappedCount(rows)).toBe(0)
    expect(rows[0].source).toEqual({
      cluster: "iad-1",
      url: SHARED,
      kind: "prometheus",
      label: "iad-1",
    })
  })

  it("still reports a cluster with no label as unmapped", () => {
    const rows = sourceRows([], [{ id: "clu_2", name: "sjc-1" }], SHARED)
    expect(unmappedCount(rows)).toBe(1)
  })

  it("reports a labelled cluster unmapped when there is no shared store", () => {
    expect(unmappedCount(sourceRows([], LABELLED, ""))).toBe(1)
  })

  it("lets an explicit entry win over the label", () => {
    const rows = sourceRows([source("iad-1")], LABELLED, SHARED)
    expect(rows[0].source?.url).not.toBe(SHARED)
    expect(rows).toHaveLength(1)
  })
})
