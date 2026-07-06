import { render, screen } from "@/test/render"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { TopologyChoice } from "./topology-declare"
import type { WorkloadRole } from "@/modules/demand/types"

const ROLES: WorkloadRole[] = [
  { role: "prefill", weight: 1, engine_args: {}, max_batch_tokens: 32_768 },
  { role: "decode", weight: 2, engine_args: {}, max_batch_tokens: 2048 },
]

describe("declaring the split", () => {
  it("starts aggregated, which is what almost everything is", () => {
    render(
      <TopologyChoice
        roles={null}
        onChange={() => {}}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
      />
    )
    expect(screen.getByText("Aggregated")).toBeTruthy()
    expect(screen.queryByText("Workers per group")).toBeNull()
  })

  it("lands the whole recipe on one click", async () => {
    // Choosing disaggregated must not be the start of a form-filling exercise:
    // the common case is NVIDIA's own 1:2 with its per-role arguments.
    const onChange = vi.fn()
    render(
      <TopologyChoice
        roles={null}
        onChange={onChange}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
      />
    )
    await userEvent.click(screen.getByText("Disaggregated"))
    const roles = onChange.mock.calls[0][0] as WorkloadRole[]
    expect(roles.map((r) => [r.role, r.weight])).toEqual([
      ["prefill", 1],
      ["decode", 2],
    ])
    expect(roles[0].engine_args["enforce-eager"]).toBe("")
  })

  it("prices the group in devices, not in workers", () => {
    // 8 is devices per WORKER and 24 is what placement will refuse it in.
    render(
      <TopologyChoice
        roles={ROLES}
        onChange={() => {}}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
      />
    )
    expect(
      screen.getByText("one group = 24 b300-288gb-sxm · 3 workers")
    ).toBeTruthy()
  })

  it("says what a ratio that does not divide the fleet will strand", () => {
    // Four nodes cannot express 1:2 at TP8. Nothing refuses it — not the
    // declaration, not placement — so this line is the only warning there is.
    render(
      <TopologyChoice
        roles={ROLES}
        onChange={() => {}}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
        budgetDevices={32}
      />
    )
    expect(
      screen.getByText(/1 unit fits and 8 devices would sit idle/)
    ).toBeTruthy()
  })

  it("stays quiet when the group divides the fleet", () => {
    render(
      <TopologyChoice
        roles={ROLES}
        onChange={() => {}}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
        budgetDevices={48}
      />
    )
    expect(screen.queryByText(/sit idle/)).toBeNull()
  })

  it("will not step a weight below one", async () => {
    // A role with no workers is not a role, and the plane refuses it. The
    // control refuses it first, where the person can still see why.
    const onChange = vi.fn()
    render(
      <TopologyChoice
        roles={ROLES}
        onChange={onChange}
        deviceCount={8}
        deviceClass="b300-288gb-sxm"
      />
    )
    expect(
      screen
        .getByRole("button", { name: "fewer prefill" })
        .hasAttribute("disabled")
    ).toBe(true)
    await userEvent.click(screen.getByRole("button", { name: "fewer decode" }))
    expect(onChange.mock.calls[0][0][1].weight).toBe(1)
  })
})
