import { describe, expect, it } from "vitest"

import { fixtureTransport } from "@/lib/api/fixtures"
import { InvalidError } from "@/lib/api/transport"
import type { ArtifactUsage } from "@/modules/demand/types"
import type { FleetSummary } from "@/modules/overview/types"
import "./index"
import { artifacts, devices, workloads } from "./data"

const get = <T>(path: string, query?: Record<string, string | string[]>) =>
  fixtureTransport.request<T>("GET", path, { query })

describe("offline artifact usage", () => {
  it("routes `/artifacts/usage` to the batched read, not to a ref", async () => {
    const ids = artifacts.slice(0, 3).map((a) => a.id)
    const batch = await get<{ items: ArtifactUsage[] }>("/artifacts/usage", {
      id: [...ids, "art_unknown"],
    })
    expect(batch.items.map((item) => item.artifact_id)).toEqual(ids)
  })

  it("counts the same workloads one at a time as in a batch", async () => {
    const served = artifacts.find((a) =>
      workloads.some((w) => w.serving.artifact_id === a.id)
    )!
    const one = await get<ArtifactUsage>(`/artifacts/${served.name}/usage`)
    const batch = await get<{ items: ArtifactUsage[] }>("/artifacts/usage", {
      id: served.id,
    })
    expect(one.workload_count).toBeGreaterThan(0)
    expect(batch.items).toEqual([one])
  })

  it("refuses a batch that names nothing", async () => {
    await expect(get("/artifacts/usage")).rejects.toThrow(InvalidError)
  })
})

describe("offline fleet summary", () => {
  it("adds up the device classes to the fleet total", async () => {
    const summary = await get<FleetSummary>("/fleet/summary")
    expect(summary.devices.total).toBe(devices.length)
    expect(
      summary.devices.by_class.reduce((sum, row) => sum + row.allocated, 0)
    ).toBe(summary.devices.allocated)
    expect(summary.workloads.total).toBe(workloads.length)
  })
})
