import { describe, expect, it } from "vitest"

import { fixtureTransport } from "@/lib/api/fixtures"
import { InvalidError } from "@/lib/api/transport"
import type { ArtifactRefresh } from "@/modules/demand/types"
import "./index"
import { artifacts, deviceClasses } from "./data"
import { fitment } from "./fitment"

const refresh = (ref: string) =>
  fixtureTransport.request<ArtifactRefresh>(
    "POST",
    `/artifacts/${ref}/refresh`,
    { body: {} }
  )

/**
 * The fixture mutates the artifact, so these run in order on purpose: the shape
 * arrives on the first re-read and the second has nothing left to do. That is the
 * sequence a person performs, and testing them independently would miss the case
 * where the second press reports the same diff again.
 */
describe("offline artifact refresh", () => {
  it("records the shape an artifact was registered without", async () => {
    const before = fitment(artifacts[1], deviceClasses(), {})
    expect(before.items.every((item) => !item.kv_counted)).toBe(true)

    const result = await refresh("bge-m3-embed")
    expect(result.source).toBe("hf://BAAI/bge-m3")
    expect(result.changed).toContain("metadata.shape")
    expect(result.changed).toContain("metadata.context_length")
  })

  it("prices the context window that was only a floor before", async () => {
    const after = fitment(artifacts[1], deviceClasses(), {})
    expect(after.items.every((item) => item.kv_counted)).toBe(true)
    expect(after.context_length).toBe(8192)
    expect(after.note).toBeNull()
    expect(after.items[0].required_gb).toBeGreaterThan(
      after.items[0].weights_gb
    )
  })

  it("reports an unchanged source as a success with an empty diff", async () => {
    const result = await refresh("bge-m3-embed")
    expect(result.changed).toEqual([])
  })

  it("refuses an artifact whose bytes came from somewhere we cannot ask", async () => {
    await expect(refresh("llama-3.3-70b-instruct")).rejects.toThrow(
      InvalidError
    )
  })
})
