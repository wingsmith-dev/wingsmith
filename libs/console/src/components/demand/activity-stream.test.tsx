import { render, screen } from "@/test/render"
import { describe, expect, it } from "vitest"
import userEvent from "@testing-library/user-event"

import type { PlaneEvent } from "@/modules/machinery/types"
import type { WorkloadDeployment } from "@/modules/demand/types"
import { ActivityStream } from "./activity-stream"

function event(over: Partial<PlaneEvent> = {}): PlaneEvent {
  return {
    id: "evt_1",
    cluster_id: "clu_1",
    kind: "deployment.pending",
    severity: "info",
    subject: { type: "deployment", id: "row-dep-1" },
    operation_id: null,
    message: "reconciling revision 1",
    details: {},
    occurred_at: "2026-08-31T00:38:46Z",
    recorded_at: "2026-08-31T00:38:46Z",
    ...over,
  }
}

const DEPLOYMENTS = [
  { id: "row-dep-1", cluster_name: "ap1" },
  { id: "row-dep-2", cluster_name: "ap2" },
] as WorkloadDeployment[]

/**
 * The failure this file exists for: the tab rendered eight hard-coded lines
 * about replicas that never existed, beside an events panel that was always
 * empty. Every assertion here is that what is on screen came from the plane.
 */
describe("ActivityStream", () => {
  it("shows the reason a download is stalled", () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[
          event({
            id: "evt_cache",
            kind: "cache.absent",
            subject: { type: "artifact", id: "row-art-1" },
            message: "no recipe on this cluster names an engine image",
            details: { reason: "NoRuntimeImage" },
          }),
        ]}
      />
    )
    expect(
      screen.getByText(/no recipe on this cluster names an engine image/)
    ).toBeInTheDocument()
  })

  it("says nothing has happened rather than inventing lines", () => {
    render(
      <ActivityStream loading={false} deployments={DEPLOYMENTS} events={[]} />
    )
    expect(screen.getByText(/Nothing has been recorded/)).toBeInTheDocument()
  })

  it("narrows to one cluster's deployment", async () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[
          event({ id: "a", message: "ap1 is waiting on weights" }),
          event({
            id: "b",
            subject: { type: "deployment", id: "row-dep-2" },
            message: "ap2 is serving",
          }),
        ]}
      />
    )

    await userEvent.selectOptions(screen.getByRole("combobox"), "row-dep-1")

    expect(screen.getByText(/ap1 is waiting on weights/)).toBeInTheDocument()
    expect(screen.queryByText(/ap2 is serving/)).not.toBeInTheDocument()
  })

  it("matches a deployment by the public id its details carry", async () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[
          event({
            id: "c",
            kind: "deployment.dispatched",
            subject: { type: "deployment", id: null },
            details: { deployment_id: "row-dep-2" },
            message: "sent to ap2",
          }),
        ]}
      />
    )

    await userEvent.selectOptions(screen.getByRole("combobox"), "row-dep-2")

    expect(screen.getByText(/sent to ap2/)).toBeInTheDocument()
  })

  it("keeps the weights in their own bucket, not duplicated per deployment", async () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[
          event({
            id: "d",
            kind: "cache.fetching",
            subject: { type: "artifact", id: "row-art-1" },
            message: "the download is running",
          }),
        ]}
      />
    )

    await userEvent.selectOptions(screen.getByRole("combobox"), "row-dep-1")
    expect(screen.queryByText(/the download is running/)).not.toBeInTheDocument()

    await userEvent.selectOptions(screen.getByRole("combobox"), "weights")
    expect(screen.getByText(/the download is running/)).toBeInTheDocument()
  })

  it("filters by severity so a failure is one click away", async () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[
          event({ id: "e", message: "routine progress" }),
          event({ id: "f", severity: "error", message: "the render failed" }),
        ]}
      />
    )

    await userEvent.click(screen.getByRole("button", { name: "error" }))

    expect(screen.getByText(/the render failed/)).toBeInTheDocument()
    expect(screen.queryByText(/routine progress/)).not.toBeInTheDocument()
  })

  it("counts what is shown against what was returned", () => {
    render(
      <ActivityStream
        loading={false}
        deployments={DEPLOYMENTS}
        events={[event({ id: "g" }), event({ id: "h" })]}
      />
    )
    expect(screen.getByText("2 of 2")).toBeInTheDocument()
  })
})
