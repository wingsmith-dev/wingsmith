import { describe, expect, it } from "vitest"

import { artifacts } from "@/modules/offline/data"
import { artifactSource, refreshSummary } from "./display"
import type { Artifact } from "./types"

const llama = artifacts[0]
const embed = artifacts[1]

function withSource(patch: Partial<Artifact>): Artifact {
  return { ...embed, ...patch }
}

describe("artifactSource", () => {
  it("reads the slug the plane recorded at resolve time", () => {
    expect(artifactSource(embed)).toBe("hf://BAAI/bge-m3")
  })

  it("falls back to an hf location and drops the revision it was pinned at", () => {
    const artifact = withSource({
      metadata: {},
      locations: [
        {
          scheme: "hf",
          uri: `hf://openai/gpt-oss-20b@${"c".repeat(40)}`,
          region: null,
          preferred: true,
        },
      ],
    })
    expect(artifactSource(artifact)).toBe("hf://openai/gpt-oss-20b")
  })

  it("has nothing to offer for weights that came from somewhere else", () => {
    expect(artifactSource(llama)).toBeNull()
    expect(
      artifactSource(withSource({ metadata: {}, locations: [] }))
    ).toBeNull()
  })

  it("does not mistake a bare word for a repo", () => {
    expect(
      artifactSource(withSource({ metadata: { huggingface_id: "bge-m3" } }))
    ).toBeNull()
  })
})

describe("refreshSummary", () => {
  it("says so when the source had nothing new, rather than going quiet", () => {
    expect(refreshSummary([])).toContain("nothing changed")
  })

  it("drops the metadata prefix, which carries no information by then", () => {
    expect(refreshSummary(["parameter_count", "metadata.shape"])).toBe(
      "updated parameter_count, shape"
    )
  })
})
