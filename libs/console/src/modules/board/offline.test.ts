import { describe, expect, it } from "vitest"
import {
  allocations,
  clusters,
  devices,
  domains,
  nodes,
} from "@/modules/offline/data"
import { buildBoard, isFree, liveAssignments } from "./build"
import { metrics } from "./sandbox"

/**
 * The board over the demo fleet.
 *
 * The offline corpus is the only place the console can be read end to end
 * without a plane, so it is also the only place these invariants can be checked
 * against something fleet-shaped: a held device is not also allocatable, a slice
 * is not also a spare card, and the tallies are the leaves and not the cards.
 */
function board() {
  return buildBoard({
    clusters,
    devices,
    domains,
    nodes,
    assignments: liveAssignments(allocations),
  })
}

describe("the offline board", () => {
  it("spans every region the fleet reports", () => {
    const map = board()
    expect(map.regions.map((region) => region.region)).toEqual([
      "eu-central-1",
      "eu-west-1",
      "us-east-1",
      "us-west-2",
    ])
    expect(map.regions.every((region) => region.clusters.length > 0)).toBe(true)
  })

  it("never counts a held device as free", () => {
    for (const cell of board().devices.values()) {
      if (cell.holder) expect(cell.device.allocatable).toBe(false)
      if (isFree(cell)) expect(cell.holder).toBeNull()
    }
  })

  it("counts leaves, so a partitioned card is its slices", () => {
    const map = board()
    const leaves = [...map.devices.values()].filter(
      (cell) => cell.slices.length === 0
    )
    const parents = [...map.devices.values()].filter(
      (cell) => cell.slices.length > 0
    )

    expect(parents.length).toBeGreaterThan(0)
    expect(map.counts.total).toBe(leaves.length)
    expect(parents.every((cell) => !isFree(cell))).toBe(true)
    const counts = map.counts
    expect(counts.held + counts.free + counts.fault + counts.cordoned).toBe(
      counts.total
    )
  })

  it("holds something in more than one region, and leaves a gang's worth free", () => {
    const map = board()
    const holding = map.regions.filter((region) => region.counts.held > 0)

    expect(holding.length).toBeGreaterThan(1)
    expect(metrics(map).largest_block).toBeGreaterThanOrEqual(8)
  })
})
