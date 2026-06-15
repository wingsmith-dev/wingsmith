import { describe, expect, it } from "vitest"
import { fixtureTransport } from "@/lib/api/fixtures"
import type { Reservation } from "@/modules/placement/types"
import "@/modules/offline"
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
function board(reservations: Reservation[] = []) {
  return buildBoard({
    clusters,
    devices,
    domains,
    nodes,
    assignments: liveAssignments(allocations),
    reservations,
  })
}

const owed = () =>
  fixtureTransport.request<Reservation[]>("GET", "/placement/reservations")

describe("the offline board", () => {
  it("spans every region the fleet reports, the empty ones last", () => {
    const map = board()
    expect(map.regions.map((region) => region.region)).toEqual([
      "eu-west-1",
      "us-east-1",
      "us-west-2",
      "eu-central-1",
    ])
    expect(map.regions.every((region) => region.clusters.length > 0)).toBe(true)
    expect(map.regions[3].counts.total).toBe(0)
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

/**
 * The demo fleet has to carry a converging deployment, or the reserved numerals
 * are unreachable without a plane and nobody ever sees the state they exist to
 * explain.
 */
describe("the offline board's reservations", () => {
  it("owes something somewhere, and owes it to a named deployment", async () => {
    const rows = await owed()

    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(row.devices).toBeGreaterThan(0)
      expect(row.deployment).toBeTruthy()
      expect(row.workload_name).toBeTruthy()
      expect(clusters.some((entry) => entry.id === row.cluster)).toBe(true)
    }
  })

  it("counts the exact promise on the cluster and the painted cells below it", async () => {
    const rows = await owed()
    const map = board(rows)
    const total = rows.reduce((sum, row) => sum + row.devices, 0)
    const painted = [...map.devices.values()].filter(
      (cell) => cell.holder?.origin === "reserved"
    ).length

    expect(map.counts.reserved).toBe(total)
    expect(map.counts.available).toBeLessThanOrEqual(map.counts.free)
    /**
     * At most, because the demo fleet owes more than it has free — which is the
     * case worth pinning: the cluster reports the true number and the board
     * paints what it can, and the two are allowed to differ.
     */
    expect(painted).toBeLessThanOrEqual(total)
    expect(painted).toBeGreaterThan(0)
  })

  it("keeps a painted cell out of what anything could still take", async () => {
    const rows = await owed()
    const map = board(rows)

    for (const cell of map.devices.values()) {
      if (cell.holder?.origin !== "reserved") continue
      expect(isFree(cell)).toBe(false)
      expect(cell.device.allocatable).toBe(true)
    }
  })

  it("answers the cluster filter the board scopes it with", async () => {
    const rows = await owed()
    const target = rows[0].cluster
    const scoped = await fixtureTransport.request<Reservation[]>(
      "GET",
      "/placement/reservations",
      { query: { cluster: target } }
    )

    expect(scoped.length).toBeGreaterThan(0)
    expect(scoped.every((row) => row.cluster === target)).toBe(true)
  })
})
