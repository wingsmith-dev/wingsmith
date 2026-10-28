import { fireEvent, render, screen } from "@/test/render"
import type {
  Artifact,
  ArtifactFit,
  ArtifactFitLayout,
  ArtifactFitment,
  ServingFootprint,
} from "@/modules/demand/types"
import { describe, expect, it, vi } from "vitest"
import { ArtifactFitmentPanel } from "./artifact-fitment"

const fitment = vi.fn()

vi.mock("@/modules/demand/queries", () => ({
  useArtifactFitment: () => fitment(),
}))

vi.mock("./artifact-refresh", () => ({
  RefreshSourceButton: () => null,
}))

const ARTIFACT: Artifact = {
  id: "art_1",
  digest: "sha256:abc",
  verified: true,
  name: "openai/gpt-oss-120b",
  kind: "weights",
  format: "safetensors",
  size_bytes: 65_248_893_184,
  licence: "apache-2.0",
  parameter_count: 116_829_156_672,
  base_artifact_id: null,
  project_id: null,
  metadata: { context_length: 131072, quantization: "mxfp4" },
  created_at: "2026-08-01T10:00:00Z",
  updated_at: null,
  revision: "1",
  locations: [],
}

/** 57 GB of weights, plus 36 KiB of KV per token for each of 128 sequences. */
function layout(
  device_count: number,
  context_length: number,
  overrides: Partial<ArtifactFitLayout> = {}
): ArtifactFitLayout {
  return {
    device_count,
    node_count: 1,
    tensor_parallel: device_count,
    pipeline_parallel: 1,
    aggregate_gb: device_count * 125,
    required_gb: Math.round(57 + (128 * context_length * 36_864) / 1024 ** 3),
    context_length,
    reduced: context_length < 131072,
    supply_short: false,
    reason: `needs ${device_count}x H200 in one node`,
    ...overrides,
  }
}

const FIT: ArtifactFit = {
  device_class: "h200-141gb-sxm",
  label: "NVIDIA H200 141GB SXM",
  fits: true,
  possible: true,
  reduced: false,
  device_count: 8,
  node_count: 1,
  devices_per_node: 8,
  tensor_parallel: 8,
  pipeline_parallel: 1,
  memory_mib_per_device: 143771,
  aggregate_gb: 1003,
  weights_gb: 57,
  required_gb: 633,
  context_length: 131072,
  kv_counted: true,
  reason: "needs 8x H200 at the full 131,072 tokens",
  devices_total: 8,
  devices_allocatable: 8,
  cluster_count: 1,
  supply_short: false,
  in_catalog: true,
  layouts: [
    layout(2, 32768),
    layout(4, 65536),
    layout(8, 131072),
    layout(16, 131072, { node_count: 2, supply_short: true }),
  ],
}

function response(fit: ArtifactFit = FIT): ArtifactFitment {
  return {
    artifact_id: ARTIFACT.id,
    scope: "fleet",
    sizable: true,
    quantization: "mxfp4",
    context_length: 131072,
    concurrency: 128,
    max_devices: 512,
    considered: 1,
    note: null,
    items: [fit],
  }
}

function panel(selected: ServingFootprint | null, onPick = vi.fn()) {
  fitment.mockReturnValue({ data: response(), isLoading: false, error: null })
  render(
    <ArtifactFitmentPanel
      artifact={ARTIFACT}
      mode="select"
      selected={selected}
      onPick={onPick}
    />
  )
  return onPick
}

describe("ArtifactFitmentPanel, choosing how many cards", () => {
  it("picks the recommended layout when the class row is clicked", () => {
    const onPick = panel(null)

    fireEvent.click(screen.getByText("NVIDIA H200 141GB SXM"))

    expect(onPick).toHaveBeenCalledTimes(1)
    const [footprint] = onPick.mock.calls[0]
    expect(footprint.device_count).toBe(8)
    expect(footprint.context_length).toBe(131072)
    expect(footprint.quantization).toBe("mxfp4")
  })

  it("keeps the other counts out of the way until a class is chosen", () => {
    panel(null)
    expect(screen.queryByText("How many")).not.toBeInTheDocument()
  })

  it("offers the other counts once a class is chosen", () => {
    const onPick = panel({
      device_class: "h200-141gb-sxm",
      device_count: 8,
      tensor_parallel: 8,
      pipeline_parallel: 1,
      context_length: 131072,
      quantization: "mxfp4",
    })

    expect(screen.getByText("How many")).toBeInTheDocument()
    fireEvent.click(screen.getByText("4 dev"))

    const [footprint] = onPick.mock.calls[0]
    expect(footprint.device_count).toBe(4)
    expect(footprint.tensor_parallel).toBe(4)
    expect(footprint.context_length).toBe(65536)
  })

  it("offers a row the sizing does not like, because that is an estimate", () => {
    /**
     * The complaint this answers. gemma-4-26b was sized as if all sixty-two of its
     * layers held the full 131k window when fifty-one of them hold a thousand tokens,
     * so every row that would have served it came back as a no-fit and every one of
     * them was unclickable. An estimate that refuses is worse than one that warns: a
     * shape it wrongly allows fails to start and says why, and a shape it wrongly
     * refuses is a model nobody can deploy.
     */
    fitment.mockReturnValue({
      data: response({
        ...FIT,
        fits: false,
        possible: true,
        reason: "8x H200 holds the weights but not a usable context",
      }),
      isLoading: false,
      error: null,
    })
    const onPick = vi.fn()
    render(
      <ArtifactFitmentPanel
        artifact={ARTIFACT}
        mode="select"
        selected={null}
        onPick={onPick}
      />
    )

    fireEvent.click(screen.getByText("NVIDIA H200 141GB SXM"))
    expect(onPick).toHaveBeenCalledTimes(1)
  })

  it("still refuses a row the arithmetic rules out, which the plane refuses too", () => {
    /**
     * `possible` is where the line is: weights that do not sit on the cards, heads that
     * do not divide across the tensor group. The plane's `check_capacity` rejects those
     * on submit, so offering one here is a dead end instead of a choice.
     */
    fitment.mockReturnValue({
      data: response({
        ...FIT,
        fits: false,
        possible: false,
        reason:
          "the weights need 218 GB at fp16 and 1x H200 offers 125 GB usable",
      }),
      isLoading: false,
      error: null,
    })
    const onPick = vi.fn()
    render(
      <ArtifactFitmentPanel
        artifact={ARTIFACT}
        mode="select"
        selected={null}
        onPick={onPick}
      />
    )

    fireEvent.click(screen.getByText("NVIDIA H200 141GB SXM"))
    expect(onPick).not.toHaveBeenCalled()
  })

  it("shows a layout the fleet is too small for and refuses to offer it", () => {
    const onPick = panel({
      device_class: "h200-141gb-sxm",
      device_count: 8,
      tensor_parallel: 8,
      pipeline_parallel: 1,
      context_length: 131072,
      quantization: "mxfp4",
    })

    fireEvent.click(screen.getByText("2 nodes x 8"))
    expect(onPick).not.toHaveBeenCalled()
  })
})

describe("ArtifactFitmentPanel, the window it sizes against", () => {
  it("shows the caller's window when the caller owns it", () => {
    fitment.mockReturnValue({ data: response(), isLoading: false, error: null })
    render(
      <ArtifactFitmentPanel
        artifact={ARTIFACT}
        mode="select"
        selected={null}
        contextLength={800_000}
        onContextLengthChange={vi.fn()}
      />
    )

    expect(screen.getByText("800,000")).toBeInTheDocument()
  })

  it("keeps a window that is not a halving of the declared maximum", () => {
    fitment.mockReturnValue({ data: response(), isLoading: false, error: null })
    render(
      <ArtifactFitmentPanel
        artifact={ARTIFACT}
        mode="select"
        selected={null}
        contextLength={100_000}
        onContextLengthChange={vi.fn()}
      />
    )

    expect(screen.getByText("100,000")).toBeInTheDocument()
    expect(screen.queryByText("131,072")).not.toBeInTheDocument()
  })

  it("falls back to the plane's answer when nobody owns the window", () => {
    fitment.mockReturnValue({ data: response(), isLoading: false, error: null })
    render(
      <ArtifactFitmentPanel artifact={ARTIFACT} mode="select" selected={null} />
    )

    expect(screen.getByText("131,072")).toBeInTheDocument()
  })
})
