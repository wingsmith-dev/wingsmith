import { fireEvent, render, screen } from "@/test/render"
import type {
  Workload,
  WorkloadDeployment,
  WorkloadRevision,
  WorkloadServingUpdate,
} from "@/modules/demand/types"
import { describe, expect, it, vi } from "vitest"
import { WorkloadRevisions } from "./workload-revisions"

const revisions = vi.fn()
const update = vi.fn()
const annotate = vi.fn()

vi.mock("@/modules/demand/queries", () => ({
  useWorkloadRevisions: () => revisions(),
  useUpdateWorkload: () => ({
    mutate: update,
    isPending: false,
    error: null,
  }),
  useAnnotateWorkloadRevision: () => ({
    mutate: annotate,
    isPending: false,
    error: null,
  }),
}))

const DECLARED = "svg_current"
const OLD = "svg_previous"

function body(fields: Record<string, unknown>): WorkloadServingUpdate {
  return fields as WorkloadServingUpdate
}

function revision(over: Partial<WorkloadRevision> = {}): WorkloadRevision {
  return {
    id: "wkr_1",
    number: 1,
    serving_digest: OLD,
    artifact: "art_1",
    reason: "created",
    note: null,
    created_at: "2026-09-01T10:00:00Z",
    serving: body({ context_length: 8192 }),
    ...over,
  }
}

function deployment(live: Record<string, number>): WorkloadDeployment {
  return { live_digests: live } as WorkloadDeployment
}

function workload(deployments: WorkloadDeployment[] = []): Workload {
  return {
    id: "wkl_1",
    name: "llama-prod",
    serving_digest: DECLARED,
    revision: "1700000000",
    serving: { artifact_id: "art_1" },
    deployments,
  } as unknown as Workload
}

const HISTORY = [
  revision({
    id: "wkr_2",
    number: 2,
    serving_digest: DECLARED,
    reason: "updated",
    serving: body({ context_length: 16_384 }),
  }),
  revision(),
]

function list(items: WorkloadRevision[]) {
  return {
    items,
    loaded: items.length,
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }
}

describe("WorkloadRevisions", () => {
  it("names what moved between one revision and the one before it", () => {
    revisions.mockReturnValue(list(HISTORY))
    render(<WorkloadRevisions workload={workload()} />)

    expect(screen.getByText("context_length")).toBeInTheDocument()
    expect(screen.getByText("8192")).toBeInTheDocument()
    expect(screen.getByText("16384")).toBeInTheDocument()
  })

  it("separates what is declared from what is actually running", () => {
    revisions.mockReturnValue(list(HISTORY))
    render(
      <WorkloadRevisions
        workload={workload([deployment({ [DECLARED]: 2, [OLD]: 1 })])}
      />
    )

    expect(screen.getByText("declared")).toBeInTheDocument()
    expect(screen.getByText("superseded")).toBeInTheDocument()
    expect(screen.getByText("2 live")).toBeInTheDocument()
    expect(screen.getByText("1 live")).toBeInTheDocument()
  })

  it("sends an old revision back as an ordinary serving patch", () => {
    revisions.mockReturnValue(list(HISTORY))
    render(<WorkloadRevisions workload={workload()} />)

    fireEvent.click(screen.getByLabelText("Go back to revision 1"))
    fireEvent.click(screen.getByRole("button", { name: "Apply" }))

    expect(update).toHaveBeenCalledWith(
      {
        ref: "wkl_1",
        body: { serving: { context_length: 8192 }, note: "back to v1" },
        revision: "1700000000",
      },
      expect.anything()
    )
  })

  it("refuses to go back to the configuration already in force", () => {
    revisions.mockReturnValue(list(HISTORY))
    render(<WorkloadRevisions workload={workload()} />)

    expect(screen.getByLabelText("Go back to revision 2")).toBeDisabled()
  })

  it("writes a note against the revision it was opened on", () => {
    revisions.mockReturnValue(list(HISTORY))
    render(<WorkloadRevisions workload={workload()} />)

    fireEvent.click(screen.getByLabelText("Note on revision 1"))
    fireEvent.change(screen.getByLabelText("Note"), {
      target: { value: "baseline before the fp8 move" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Save" }))

    expect(annotate).toHaveBeenCalledWith(
      {
        ref: "wkl_1",
        revision: "1",
        body: { note: "baseline before the fp8 move" },
      },
      expect.anything()
    )
  })

  it("clears a note rather than storing an empty string", () => {
    revisions.mockReturnValue(list([revision({ note: "was something" })]))
    render(<WorkloadRevisions workload={workload()} />)

    fireEvent.click(screen.getByLabelText("Note on revision 1"))
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "  " } })
    fireEvent.click(screen.getByRole("button", { name: "Save" }))

    expect(annotate).toHaveBeenCalledWith(
      expect.objectContaining({ body: { note: null } }),
      expect.anything()
    )
  })

  it("says a starting point is not a change", () => {
    revisions.mockReturnValue(list([revision()]))
    render(<WorkloadRevisions workload={workload()} />)

    expect(
      screen.getByText("The configuration it started at")
    ).toBeInTheDocument()
  })
})
