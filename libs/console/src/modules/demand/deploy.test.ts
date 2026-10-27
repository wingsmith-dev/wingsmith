import { describe, expect, it } from "vitest"

import { artifacts, workloads } from "@/modules/offline/data"
import {
  DEFAULT_ENGINE_RUNTIME_VERSION,
  derivedServingRequest,
  derivedWorkloadName,
  footprintOfServing,
  footprintSummary,
  proposedBehavior,
  proposedCapabilities,
  requiresSingleDomain,
  servingProvenance,
  slugify,
  workloadsMatching,
} from "./deploy"
import { emptyUpstream } from "./display"
import type {
  Artifact,
  ArtifactUpstream,
  ServingDefaults,
  ServingFootprint,
  UpstreamServing,
  Workload,
} from "./types"

const artifact = artifacts[0]

const claiming = (over: Partial<ArtifactUpstream>): Artifact => ({
  ...artifact,
  upstream: { ...emptyUpstream(), ...over },
})

const footprint: ServingFootprint = {
  device_class: "nvidia-h100-80gb",
  device_count: 2,
  tensor_parallel: 2,
  pipeline_parallel: 1,
  context_length: 32768,
  quantization: "fp8",
}

const defaults: ServingDefaults = {
  device_count: 2,
  device_memory_mib_min: 81_920,
  context_length: 131_072,
  quantization: null,
  dtype: "bfloat16",
}

const upstream = (over: Partial<UpstreamServing> = {}): UpstreamServing =>
  ({
    source: "vllm",
    source_uri: "https://example.invalid/recipes.yaml",
    model_id: artifact.name,
    variant: "default",
    engine: "vllm",
    engine_version_min: "0.9.1",
    nightly_required: false,
    kv_cache_dtype: "fp8",
    reasoning_parser: null,
    tool_call_parser: null,
    requires_single_topology_domain: false,
    verified_hardware: ["H200"],
    deployments: [],
    args: [],
    env: {},
    features_on: [],
    features_opt_in: [],
    strategies: [],
    ...over,
  }) as UpstreamServing

/** Only the fields `workloadsMatching` reads. */
function workloadLike(over: Partial<Workload> = {}): Workload {
  return {
    ...workloads[0],
    id: "wkl_1",
    serving: {
      ...workloads[0].serving,
      artifact_id: artifact.id,
      engine: "vllm",
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
    },
    ...over,
  } as Workload
}

describe("slugify", () => {
  it("drops the owner and normalizes the name", () => {
    expect(slugify("meta-llama/Llama-3.3-70B-Instruct")).toBe(
      "llama-3-3-70b-instruct"
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
    expect(requiresSingleDomain({ ...footprint, pipeline_parallel: 2 })).toBe(
      false
    )
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

describe("derivedServingRequest", () => {
  it("carries every field the fit determined", () => {
    const request = derivedServingRequest(artifact, footprint)
    expect(request).toMatchObject({
      artifact: artifact.id,
      context_length: 32768,
      quantization: "fp8",
      shape: {
        device_class: "nvidia-h100-80gb",
        device_count: 2,
        requires_single_topology_domain: true,
      },
      parallelism: { tensor: 2, pipeline: 1 },
      cache: { prefix_cache_enabled: true },
    })
  })

  it("groups the write shape the way the read shape is grouped", () => {
    /**
     * The plane sends and accepts one object. A flat request would be a second
     * vocabulary, and the first field renamed on the way out is a field that
     * typechecks green and arrives as undefined.
     */
    const request = derivedServingRequest(
      artifact,
      footprint
    ) as unknown as Record<string, unknown>
    for (const flat of [
      "device_class",
      "device_count",
      "tensor_parallel",
      "pipeline_parallel",
      "kv_cache_dtype",
      "behavior",
    ])
      expect(request[flat]).toBeUndefined()
  })

  it("never proposes an image, not even when the corpus names one", () => {
    /**
     * The one upstream field that must not be seeded. The image a recipe names is
     * a third party's runtime with no ai-dynamo in it, so copying it into
     * engine_runtime_image swaps a Transformers architecture error for a missing
     * dynamo.vllm module -- a different failure at the same distance from
     * serving. It reaches the form as a warning instead.
     */
    const request = derivedServingRequest(artifact, footprint, {
      defaults,
      upstream: upstream({ docker_image: "vllm/vllm-openai:glm53-flash" }),
    }) as unknown as Record<string, unknown>

    expect(request.engine_runtime_image).toBeNull()
    expect(request.frontend_image).toBeNull()
    expect(request.docker_image).toBeUndefined()
  })

  it("leaves out what nothing can derive, so the plane defaults it", () => {
    const request = derivedServingRequest(artifact, footprint)
    expect(request.max_batch_tokens).toBeUndefined()
    expect(request.resources).toBeUndefined()
    expect(request.cache?.kv_cache_fraction).toBeNull()
    expect(request.shape.topology_domain_kind).toBeNull()
  })

  it("declares no quantization for weights that ship unquantized", () => {
    /**
     * The fitment matrix sizes unquantized weights as fp16 because bytes-per-param
     * needs a number. That fallback must not reach the engine: it becomes
     * `--quantization fp16`, which no engine accepts, and the worker exits before
     * it binds a port. Empty is the correct declaration, not a missing one.
     */
    const request = derivedServingRequest(artifact, {
      ...footprint,
      quantization: "",
    })
    expect(request.quantization).toBeNull()
  })

  it("never pins the runtime tag to the engine version the corpus names", () => {
    /**
     * engine_version_min is vLLM's number and engine_runtime_version is a tag of the
     * Dynamo runtime image. Seeding one from the other would make upstream evidence
     * actively harmful: a model the corpus covered would resolve to vllm-runtime:0.9.1,
     * an address nobody publishes, while a model it did not cover would fall through to
     * latest and serve.
     */
    const request = derivedServingRequest(artifact, footprint, {
      defaults,
      upstream: upstream(),
    })
    expect(request.engine_runtime_version).toBe(DEFAULT_ENGINE_RUNTIME_VERSION)
    expect(request.engine_runtime_version).not.toBe("0.9.1")
    expect(request.cache?.kv_cache_dtype).toBe("fp8")
    expect(request.dtype).toBe("bfloat16")
  })

  it("takes what was typed over what was proposed, and empty means say nothing", () => {
    const proposal = { defaults, upstream: upstream() }
    const typed = derivedServingRequest(artifact, footprint, proposal, {
      engine_runtime_version: "1.4.1",
      dtype: "float16",
      kv_cache_dtype: "",
    })
    expect(typed.engine_runtime_version).toBe("1.4.1")
    expect(typed.dtype).toBe("float16")
    // Cleared, not reverted to the corpus's fp8: an empty field is a decision.
    expect(typed.cache?.kv_cache_dtype).toBeNull()
  })

  it("proposes no memory floor, whatever the suggestion sized", () => {
    /**
     * The failure this exists for, twice over. The plane suggested GLM-5.3-Flash as 2
     * devices at 192 GiB and the recipe's parallelism put 4 on the request, so a floor
     * carried across asked for 4 × 192 GiB to serve a 383 GiB model; re-dividing the
     * replica total fixed that one and broke the next, because gemma-4-26b was suggested
     * across 2 devices at the declared 131k window and sent to the 1 device the fit
     * chose — 122 GiB per card, from a card that has 96, refused outright.
     *
     * Both are the same mistake: the suggestion prices a configuration nobody submitted.
     * A floor is a statement about hardware, so nobody but the operator makes it.
     */
    const request = derivedServingRequest(
      artifact,
      { ...footprint, device_count: 1, tensor_parallel: 1 },
      { defaults, upstream: upstream() }
    )

    expect(request.shape.device_count).toBe(1)
    expect(request.shape.device_memory_mib_min).toBe(0)
  })

  it("carries a floor somebody states, and only that", () => {
    const request = derivedServingRequest(
      artifact,
      footprint,
      { defaults, upstream: upstream() },
      { device_memory_mib_min: 81_920 }
    )
    expect(request.shape.device_memory_mib_min).toBe(81_920)
  })

  it("treats a cleared floor as no floor rather than as a zero somebody meant", () => {
    const request = derivedServingRequest(
      artifact,
      footprint,
      { defaults, upstream: upstream() },
      { device_memory_mib_min: 0 }
    )
    expect(request.shape.device_memory_mib_min).toBe(0)
  })

  it("sends the capability under a parser somebody typed, not only one vLLM named", () => {
    /**
     * The plane refuses a tool call parser without the claim beneath it, so an override
     * that carried the parser alone would produce a request nobody can submit.
     */
    const request = derivedServingRequest(
      artifact,
      footprint,
      { upstream: upstream() },
      { tool_call_parser: "hermes" }
    )
    expect(request.serving?.tool_call_parser).toBe("hermes")
    expect(request.capabilities?.tool_calling_supported).toBe(true)
    expect(request.capabilities?.tool_choice_modes.length).toBeGreaterThan(0)
  })

  it("keeps the fit's own answers over the proposal's", () => {
    const request = derivedServingRequest(artifact, footprint, {
      defaults,
      upstream: upstream({ context_length: 131_072 }),
    })
    expect(request.context_length).toBe(32768)
    expect(request.quantization).toBe("fp8")
  })

  it("takes a typed engine over the corpus's", () => {
    const request = derivedServingRequest(
      artifact,
      footprint,
      { upstream: upstream() },
      { engine: "sglang" }
    )
    expect(request.engine).toBe("sglang")
  })
})

describe("proposedBehavior", () => {
  it("travels under the key the plane hashes, not a readable local name", () => {
    /**
     * `serving.serving` is not a slip. The column, the digest field list and the
     * HTTP body all call the behavior block `serving`, and a third name here
     * would typecheck green and decode to undefined in a browser.
     */
    const request = derivedServingRequest(artifact, footprint, {
      upstream: upstream({ tool_call_parser: "hermes" }),
    })
    expect(request.serving?.tool_call_parser).toBe("hermes")
  })

  it("carries only the parsers the corpus named", () => {
    const block = proposedBehavior(
      upstream({ tool_call_parser: "hermes", reasoning_parser: "qwen3" })
    )
    expect(block.tool_call_parser).toBe("hermes")
    expect(block.reasoning.parser).toBe("qwen3")
    expect(block.stream_usage).toBe("final_chunk")
  })

  it("is the inert block when nothing upstream names these weights", () => {
    expect(proposedBehavior(null)).toEqual(proposedBehavior(undefined))
  })
})

describe("proposedCapabilities", () => {
  it("claims the capability every proposed parser is meaningless without", () => {
    const claims = proposedCapabilities(
      upstream({ tool_call_parser: "hermes", reasoning_parser: "qwen3" })
    )
    expect(claims.tool_calling_supported).toBe(true)
    expect(claims.tool_choice_modes.length).toBeGreaterThan(0)
    expect(claims.reasoning_supported).toBe(true)
  })

  it("claims nothing when the corpus names no parser", () => {
    const claims = proposedCapabilities(upstream())
    expect(claims.tool_calling_supported).toBe(false)
    expect(claims.tool_choice_modes).toEqual([])
    expect(claims.reasoning_supported).toBe(false)
  })

  it("sends the claim with the parser, because the plane rejects one half", () => {
    const request = derivedServingRequest(artifact, footprint, {
      upstream: upstream({ tool_call_parser: "hermes" }),
    })
    expect(request.serving?.tool_call_parser).toBe("hermes")
    expect(request.capabilities?.tool_calling_supported).toBe(true)
  })
})

describe("the publisher's claims against the corpus's parsers", () => {
  it("claims what the publisher claims, with no parser needed as evidence", () => {
    const claims = proposedCapabilities(
      upstream(),
      claiming({ tool_calling: true }).upstream
    )
    expect(claims.tool_calling_supported).toBe(true)
  })

  it("drops a parser the publisher says these weights cannot use", () => {
    /**
     * Otherwise the request is unsubmittable: the plane refuses a tool call
     * parser without the capability under it, and the claim is the half that
     * somebody actually asserted.
     */
    const denied = claiming({ tool_calling: false })
    const recipe = upstream({ tool_call_parser: "hermes" })
    expect(
      proposedBehavior(recipe, denied.upstream).tool_call_parser
    ).toBeNull()
    expect(
      proposedCapabilities(recipe, denied.upstream).tool_calling_supported
    ).toBe(false)
  })

  it("reads silence as silence, so a tested parser still travels", () => {
    const silent = claiming({ tool_calling: null })
    const recipe = upstream({ tool_call_parser: "hermes" })
    expect(proposedBehavior(recipe, silent.upstream).tool_call_parser).toBe(
      "hermes"
    )
    expect(
      proposedCapabilities(recipe, silent.upstream).tool_calling_supported
    ).toBe(true)
  })

  it("carries the modalities and the output modes the publisher named", () => {
    const claims = proposedCapabilities(
      upstream(),
      claiming({
        input_modalities: ["image"],
        structured_output_modes: ["text", "json_schema"],
      }).upstream
    )
    // Text is always an input modality and the plane refuses a list without it.
    expect(claims.input_modalities).toEqual(["text", "image"])
    expect(claims.structured_output_modes).toContain("json_schema")
  })

  it("sends the artifact's claims with the request they belong to", () => {
    const request = derivedServingRequest(
      claiming({ reasoning: true }),
      footprint,
      { upstream: upstream({ reasoning_parser: "qwen3" }) }
    )
    expect(request.capabilities?.reasoning_supported).toBe(true)
    expect(request.serving?.reasoning.parser).toBe("qwen3")
  })
})

describe("servingProvenance", () => {
  it("attributes each seeded field to what proposed it", () => {
    const at = servingProvenance({ defaults, upstream: upstream() })
    expect(at.dtype).toBe("weights")
    // Never attributed to the corpus, because the corpus does not give this value:
    // engine_version_min is vLLM's number and this field is a Dynamo runtime tag.
    expect(at.engine_runtime_version).toBeUndefined()
    expect(at.kv_cache_dtype).toBe("upstream")
    expect(
      servingProvenance({ upstream: upstream({ tool_call_parser: "hermes" }) })
        .tool_calling_supported
    ).toBe("upstream")
    expect(
      servingProvenance(
        { upstream: upstream() },
        claiming({ tool_calling: true }).upstream
      ).tool_calling_supported
    ).toBe("publisher")
    expect(at.device_class).toBe("derived")
  })

  it("claims nothing when nothing was proposed", () => {
    const at = servingProvenance()
    expect(Object.values(at).every((from) => from === "derived")).toBe(true)
  })

  it("stops attributing a field once somebody has typed into it", () => {
    /**
     * "from vLLM" beside a value vLLM did not give is the one wrong label on the form
     * that is a claim about a third party, and it would survive into whatever argument
     * the configuration is later defended with.
     */
    const proposal = { defaults, upstream: upstream() }
    expect(servingProvenance(proposal).kv_cache_dtype).toBe("upstream")
    expect(
      servingProvenance(proposal, null, { kv_cache_dtype: "fp8" })
        .kv_cache_dtype
    ).toBeUndefined()
    expect(
      servingProvenance(proposal, null, { dtype: "" }).dtype
    ).toBeUndefined()
  })
})

describe("the decoded serving block", () => {
  /**
   * The names below are the plane's, not this console's. A field renamed for
   * readability is the one bug TypeScript cannot catch: the interface would
   * typecheck against every call site and the property would be `undefined` in a
   * browser, so the panel reading it renders empty and CI stays green. This test
   * is the only thing standing between that and a release.
   */
  it("reads the keys the plane actually sends", () => {
    const serving = workloads[0].serving
    for (const key of [
      "artifact_id",
      "engine",
      "engine_runtime_version",
      "engine_runtime_image",
      "frontend_image",
      "engine_args",
      "imported_args",
      "quantization",
      "dtype",
      "parallelism",
      "context_length",
      "max_batch_tokens",
      "cache",
      "serving",
      "capabilities",
      "resolved_engine_args",
      "shape",
      "resources",
      "overlay",
    ])
      expect(serving).toHaveProperty(key)
  })

  it("keeps the behavior block under `serving`, not a readable alias", () => {
    // `serving.serving` is what the column, the digest field list and the HTTP
    // body all call it. Three names for one thing is how they drift apart.
    expect(workloads[0].serving.serving.stream_usage).toBe("final_chunk")
    expect(workloads[0].serving).not.toHaveProperty("behavior")
  })

  it("carries the digest on the workload and not inside the block", () => {
    expect(workloads[0].serving_digest).toMatch(/^svg_[0-9a-f]{64}$/)
    expect(workloads[0].serving).not.toHaveProperty("serving_digest")
  })
})

describe("workloadsMatching", () => {
  it("finds a workload already running this configuration", () => {
    expect(
      workloadsMatching([workloadLike()], artifact, footprint).map((w) => w.id)
    ).toEqual(["wkl_1"])
  })

  it("does not match one that differs on a field the fit set", () => {
    const wider = workloadLike({
      serving: {
        ...workloadLike().serving,
        parallelism: { tensor: 4, pipeline: 1, expert: 1 },
      },
    })
    expect(workloadsMatching([wider], artifact, footprint)).toEqual([])
  })

  it("does not reach across artifacts", () => {
    const other = workloadLike({
      serving: { ...workloadLike().serving, artifact_id: "art_other" },
    })
    expect(workloadsMatching([other], artifact, footprint)).toEqual([])
  })

  it("treats a null quantization as shipped-as-is", () => {
    const native = workloadLike({
      serving: { ...workloadLike().serving, quantization: null },
    })
    expect(
      workloadsMatching([native], artifact, {
        ...footprint,
        quantization: "",
      })
    ).toHaveLength(1)
  })
})

describe("footprintSummary", () => {
  it("leads with the count and the label a person recognizes", () => {
    expect(footprintSummary(footprint, "H100 80GB")).toBe(
      "2 x H100 80GB · tp2 · 32k · fp8"
    )
  })

  it("reads a workload's own serving block back as a footprint", () => {
    expect(footprintSummary(footprintOfServing(workloadLike().serving))).toBe(
      "2 x nvidia-h100-80gb · tp2 · 32k · fp8"
    )
  })
})
