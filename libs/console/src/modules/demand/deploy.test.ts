import { describe, expect, it } from "vitest"

import { artifacts } from "@/modules/offline/data"
import {
  derivedRecipeName,
  derivedRecipeRequest,
  derivedWorkloadName,
  footprintSummary,
  matchExistingRecipe,
  requiresSingleDomain,
  slugify,
} from "./deploy"
import type { Recipe, RecipeFootprint } from "./types"

const artifact = artifacts[0]

const footprint: RecipeFootprint = {
  device_class: "nvidia-h100-80gb",
  device_count: 2,
  tensor_parallel: 2,
  pipeline_parallel: 1,
  context_length: 32768,
  quantization: "fp8",
}

/** Only the fields `matchExistingRecipe` reads. */
function recipeLike(over: Partial<Recipe> = {}): Recipe {
  return {
    id: "rcp_1",
    artifact_id: artifact.id,
    name: "existing",
    version: 1,
    lifecycle: "published",
    engine: "vllm",
    engine_version: "0.8.0",
    quantization: "fp8",
    context_length: 32768,
    parallelism: { tensor: 2, pipeline: 1, expert: 1 },
    shape: {
      device_class: "nvidia-h100-80gb",
      device_count: 2,
      device_memory_mib_min: 0,
      requires_single_topology_domain: true,
      topology_domain_kind: null,
    },
    ...over,
  } as Recipe
}

describe("slugify", () => {
  it("drops the owner and normalises the name", () => {
    expect(slugify("meta-llama/Llama-3.3-70B-Instruct")).toBe(
      "llama-3-3-70b-instruct"
    )
  })
})

describe("derivedRecipeName", () => {
  it("names the four things a fit determined", () => {
    expect(derivedRecipeName(artifact, footprint)).toMatch(/fp8-tp2-32k$/)
  })

  it("leaves pipeline out when it is one, and in when it is not", () => {
    expect(derivedRecipeName(artifact, footprint)).not.toContain("pp")
    expect(
      derivedRecipeName(artifact, { ...footprint, pipeline_parallel: 2 })
    ).toContain("pp2")
  })

  it("omits quantization when the weights ship as they are", () => {
    expect(
      derivedRecipeName(artifact, { ...footprint, quantization: "" })
    ).not.toContain("--")
  })

  it("distinguishes two footprints over one artifact", () => {
    const wide = { ...footprint, device_count: 4, tensor_parallel: 4 }
    expect(derivedRecipeName(artifact, footprint)).not.toBe(
      derivedRecipeName(artifact, wide)
    )
  })
})

describe("derivedWorkloadName", () => {
  it("suffixes past a name already taken", () => {
    const base = derivedWorkloadName(artifact)
    expect(derivedWorkloadName(artifact, [base])).toBe(`${base}-2`)
    expect(derivedWorkloadName(artifact, [base, `${base}-2`])).toBe(`${base}-3`)
  })
})

describe("requiresSingleDomain", () => {
  it("requires one for a tensor group wider than a device", () => {
    expect(requiresSingleDomain(footprint)).toBe(true)
  })

  it("does not, once the fit is pipeline-parallel across nodes", () => {
    expect(
      requiresSingleDomain({ ...footprint, pipeline_parallel: 2 })
    ).toBe(false)
  })

  it("does not for a single device, which has no group to split", () => {
    expect(
      requiresSingleDomain({
        ...footprint,
        device_count: 1,
        tensor_parallel: 1,
      })
    ).toBe(false)
  })
})

describe("derivedRecipeRequest", () => {
  it("carries every field the fit determined", () => {
    const request = derivedRecipeRequest(artifact, footprint)
    expect(request).toMatchObject({
      artifact: artifact.id,
      device_class: "nvidia-h100-80gb",
      device_count: 2,
      context_length: 32768,
      tensor_parallel: 2,
      pipeline_parallel: 1,
      quantization: "fp8",
      requires_single_topology_domain: true,
      prefix_cache_enabled: true,
    })
  })

  it("leaves out what it cannot derive, so the plane defaults it", () => {
    const request = derivedRecipeRequest(
      artifact,
      footprint
    ) as unknown as Record<string, unknown>
    for (const absent of [
      "dtype",
      "max_batch_tokens",
      "kv_cache_dtype",
      "kv_cache_fraction",
      "cpu_cores",
      "memory_mib",
      "disk_gib",
      "device_memory_mib_min",
      "topology_domain_kind",
    ])
      expect(request[absent]).toBeUndefined()
  })

  it("takes a typed name over the derived one", () => {
    expect(
      derivedRecipeRequest(artifact, footprint, { name: " mine " }).name
    ).toBe("mine")
  })
})

describe("matchExistingRecipe", () => {
  it("reuses a recipe with the same spec", () => {
    expect(
      matchExistingRecipe([recipeLike()], artifact, footprint)?.id
    ).toBe("rcp_1")
  })

  it("does not reuse one that differs on a field the fit set", () => {
    const wider = recipeLike({ parallelism: { tensor: 4, pipeline: 1, expert: 1 } })
    expect(matchExistingRecipe([wider], artifact, footprint)).toBeUndefined()
  })

  it("does not resurrect a deprecated recipe", () => {
    const dead = recipeLike({ lifecycle: "deprecated" })
    expect(matchExistingRecipe([dead], artifact, footprint)).toBeUndefined()
  })

  it("does not reach across artifacts", () => {
    const other = recipeLike({ artifact_id: "art_other" })
    expect(matchExistingRecipe([other], artifact, footprint)).toBeUndefined()
  })

  it("treats a null quantization as shipped-as-is", () => {
    const native = recipeLike({ quantization: null })
    expect(
      matchExistingRecipe([native], artifact, {
        ...footprint,
        quantization: "",
      })?.id
    ).toBe("rcp_1")
  })
})

describe("footprintSummary", () => {
  it("leads with the count and the label a person recognises", () => {
    expect(footprintSummary(footprint, "H100 80GB")).toBe(
      "2 x H100 80GB · tp2 · 32k · fp8"
    )
  })
})
