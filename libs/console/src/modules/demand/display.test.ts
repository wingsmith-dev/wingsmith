import { describe, expect, it } from "vitest"

import { workloads } from "@/modules/offline/data"
import {
  resolvedEngineArgs,
  servingCompanionArgs,
  workloadOperationalStatus,
} from "./display"

import type { ServingBehaviour, Workload } from "./types"

const serving = (over: Partial<ServingBehaviour> = {}): ServingBehaviour =>
  ({
    reasoning: {
      parser: null,
      default_on: false,
      transport: "reasoning_content",
    },
    stream_usage: "final_chunk",
    context_overflow: "reject",
    structured_output_backend: null,
    tool_call_parser: null,
    max_images_per_request: null,
    max_videos_per_request: null,
    ...over,
  }) as ServingBehaviour

describe("companion flags", () => {
  it("gives a modality cap the switch that turns multimodal on", () => {
    // A cap without `--enable-multimodal` caps a path the engine has switched
    // off, so the workload reads as configured and serves a text-only worker.
    expect(
      resolvedEngineArgs("vllm", {}, serving({ max_images_per_request: 4 }))
    ).toEqual({
      "limit-mm-per-prompt": '{"image": 4}',
      "enable-multimodal": "",
    })
    expect(
      resolvedEngineArgs("vllm", {}, serving({ max_videos_per_request: 1 }))
    ).toEqual({
      "limit-mm-per-prompt": '{"video": 1}',
      "enable-multimodal": "",
    })
  })

  it("renders two caps as one map and not as two flags", () => {
    expect(
      resolvedEngineArgs(
        "vllm",
        {},
        serving({ max_images_per_request: 4, max_videos_per_request: 1 })
      )["limit-mm-per-prompt"]
    ).toBe('{"image": 4, "video": 1}')
  })

  it("adds nothing on an engine that has no such switch", () => {
    expect(
      resolvedEngineArgs("sglang", {}, serving({ tool_call_parser: "qwen25" }))
    ).toEqual({
      "tool-call-parser": "qwen25",
    })
  })

  it("leaves an explicitly declared companion flag alone", () => {
    const resolved = resolvedEngineArgs(
      "vllm",
      { "--enable-multimodal": "true" },
      serving({ max_images_per_request: 4 })
    )
    expect(resolved["enable-multimodal"]).toBe("true")
    expect(resolved["limit-mm-per-prompt"]).toBe('{"image": 4}')
  })

  it("stays inert on a serving block that declares nothing", () => {
    expect(servingCompanionArgs("vllm", serving())).toEqual({})
    expect(
      resolvedEngineArgs("vllm", { "--dtype": "bfloat16" }, serving())
    ).toEqual({
      dtype: "bfloat16",
    })
  })
})

describe("workload operational status", () => {
  it("puts an observed image pull failure ahead of placement", () => {
    const base = workloads[0]
    const deployment = base.deployments[0]
    const broken: Workload = {
      ...base,
      phase: "placed",
      scale: { ...base.scale, desired: 1, placed: 1, ready: 0 },
      deployments: [
        {
          ...deployment,
          admission_phase: "placed",
          desired_replicas: 1,
          placed_at: "2026-09-02T12:00:00Z",
          replicas: { ...deployment.replicas, ready: 0 },
          status: {
            observed_generation: deployment.generation,
            phase: "failed",
            reason: "ImagePullFailed",
            message: "0 of 1 replicas ready",
            desired_replicas: 1,
            ready_replicas: 0,
            updated_replicas: 0,
            unavailable_replicas: 1,
            last_error_code: "ImagePullFailed",
            last_error_message: "back-off pulling image",
            last_error_at: "2026-09-02T12:01:00Z",
            observed_at: "2026-09-02T12:01:00Z",
          },
        },
      ],
    }

    expect(workloadOperationalStatus(broken)).toMatchObject({
      phase: "failed",
      tone: "danger",
      reason: "The cluster could not pull an image",
      placement: "placed",
      needsAttention: true,
    })
  })

  it("leaves an ordinary placed rollout quiet", () => {
    const base = workloads[0]
    const placed: Workload = {
      ...base,
      phase: "placed",
      deployments: base.deployments.map((deployment) => ({
        ...deployment,
        admission_phase: "placed",
        status: null,
      })),
    }

    expect(workloadOperationalStatus(placed)).toMatchObject({
      phase: "placed",
      tone: "info",
      reason: null,
      needsAttention: false,
    })
  })
})
