import { describe, expect, it } from "vitest"

import { workloads } from "@/modules/offline/data"
import { servingArgs, workloadOperationalStatus } from "./display"

import type { ServingBehavior, Workload } from "./types"

const serving = (over: Partial<ServingBehavior> = {}): ServingBehavior =>
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
  }) as ServingBehavior

describe("modality caps", () => {
  it("renders two caps as one map and not as two flags", () => {
    expect(
      servingArgs(
        "vllm",
        serving({ max_images_per_request: 4, max_videos_per_request: 1 })
      )["limit-mm-per-prompt"]
    ).toBe('{"image": 4, "video": 1}')
  })

  it("renders nothing on a serving block that declares nothing", () => {
    expect(servingArgs("vllm", serving())).toEqual({})
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
      phase: "starting",
      tone: "info",
      reason: null,
      needsAttention: false,
    })
  })

  it("reads a deployment waiting on weights as downloading, not failed", () => {
    const base = workloads[0]
    const deployment = base.deployments[0]
    const loading: Workload = {
      ...base,
      deployments: [
        {
          ...deployment,
          admission_phase: "placed",
          status: {
            observed_generation: deployment.generation,
            phase: "pending",
            reason: "WeightsNotResident",
            message: null,
            desired_replicas: 1,
            ready_replicas: 0,
            updated_replicas: 0,
            unavailable_replicas: 1,
            roles: [],
            last_error_code: null,
            last_error_message: null,
            last_error_at: null,
            observed_at: "2026-09-02T12:01:00Z",
          },
        },
      ],
    }
    expect(workloadOperationalStatus(loading)).toMatchObject({
      phase: "downloading",
      needsAttention: false,
    })
  })

  it("does not call a non-fatal error code a failure", () => {
    const base = workloads[0]
    const deployment = base.deployments[0]
    const crashing: Workload = {
      ...base,
      deployments: [
        {
          ...deployment,
          admission_phase: "placed",
          status: {
            observed_generation: deployment.generation,
            phase: "placed",
            reason: "CrashLooping",
            message: null,
            desired_replicas: 1,
            ready_replicas: 0,
            updated_replicas: 0,
            unavailable_replicas: 1,
            roles: [],
            last_error_code: "CrashLooping",
            last_error_message: "back-off",
            last_error_at: "2026-09-02T12:01:00Z",
            observed_at: "2026-09-02T12:01:00Z",
          },
        },
      ],
    }
    expect(workloadOperationalStatus(crashing)).toMatchObject({
      phase: "blocked",
      needsAttention: true,
    })
  })

  it("reads a worker loading weights as starting, not blocked", () => {
    const base = workloads[0]
    const deployment = base.deployments[0]
    const loading: Workload = {
      ...base,
      deployments: [
        {
          ...deployment,
          admission_phase: "placed",
          status: {
            observed_generation: deployment.generation,
            phase: "placed",
            reason: "ComponentNotReady",
            message: null,
            desired_replicas: 1,
            ready_replicas: 0,
            updated_replicas: 0,
            unavailable_replicas: 1,
            roles: [],
            last_error_code: "ComponentNotReady",
            last_error_message: "Deployment is not ready",
            last_error_at: "2026-09-02T12:01:00Z",
            observed_at: "2026-09-02T12:01:00Z",
          },
        },
      ],
    }
    expect(workloadOperationalStatus(loading)).toMatchObject({
      phase: "starting",
      needsAttention: false,
    })
  })
})
