import { render, screen } from "@/test/render"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

/**
 * The mutation, mocked at the hook rather than over HTTP.
 *
 * What these tests are about is the BODY the panel builds — a ratio change that
 * drops a role's arguments is a silent reload, and the only place that is
 * visible is the object handed to the mutation.
 */
const mutate = vi.fn()
vi.mock("@/modules/demand/queries", () => ({
  useUpdateWorkload: () => ({ mutate, isPending: false }),
}))

import { RolesPanel } from "./serving-topology"
import type { Workload } from "@/modules/demand/types"

/** Declared 1:2 over two groups; one prefill up against four decode. */
function kimi(over: Partial<Workload> = {}): Workload {
  return {
    id: "wkl_k",
    name: "kimi-k3-disagg",
    revision: "rev_1",
    scale: {
      unit: "replicas",
      min: 1,
      max: 4,
      desired: 2,
      placed: 2,
      ready: 2,
    },
    serving: {
      shape: { device_count: 8, device_class: "b300-288gb-sxm" },
      topology: {
        serving_topology: "disaggregated",
        group_devices: 24,
        group_nodes_hint: "",
        roles: [
          {
            role: "prefill",
            weight: 1,
            engine_args: { "enforce-eager": "" },
            max_batch_tokens: 32_768,
          },
          {
            role: "decode",
            weight: 2,
            engine_args: {},
            max_batch_tokens: 2048,
          },
        ],
      },
    },
    deployments: [
      {
        id: "dep_1",
        cluster_name: "usw2",
        status: {
          roles: [
            { role: "prefill", desired: 2, ready: 1 },
            { role: "decode", desired: 4, ready: 4 },
          ],
        },
      },
    ],
    ...over,
  } as unknown as Workload
}

beforeEach(() => mutate.mockReset())

describe("the roles panel", () => {
  it("names both ends of the declared ratio and what a group costs", () => {
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("1 prefill : 2 decode")).toBeTruthy()
    expect(screen.getByText("24× b300-288gb-sxm")).toBeTruthy()
  })

  it("shows an achieved ratio that disagrees, and calls it settling", () => {
    // Not an error and not styled as one. The quotient is derived from observed
    // counts, so it is allowed to disagree with the declaration while the roles
    // come up at different speeds.
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("1 prefill : 4 decode on usw2")).toBeTruthy()
    expect(screen.getByText("· settling")).toBeTruthy()
  })

  it("says nothing about achieved where the cluster reports no roles", () => {
    const bare = kimi({
      deployments: [
        { id: "dep_1", cluster_name: "usw2", status: null },
      ] as unknown as Workload["deployments"],
    })
    render(<RolesPanel workload={bare} />)
    expect(screen.queryByText(/achieved|settling/)).toBeNull()
  })

  it("lists only what a role overrides", () => {
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("enforce-eager=(set)")).toBeTruthy()
    expect(screen.getByText("max-num-batched-tokens=32,768")).toBeTruthy()
  })

  it("renders nothing at all for an aggregated workload", () => {
    const { container } = render(
      <RolesPanel
        workload={kimi({
          serving: {
            ...kimi().serving,
            topology: {
              serving_topology: "aggregated",
              roles: [],
              group_devices: 0,
              group_nodes_hint: "",
            },
          },
        })}
      />
    )
    expect(container.firstChild).toBeNull()
  })

  it("multiplies the ratio out into workers beside the steppers", () => {
    // Two groups at 1:2 is two prefill and four decode. The stepper says what
    // the number means before it is applied, not after.
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("→ 2 workers")).toBeTruthy()
    expect(screen.getByText("→ 4 workers")).toBeTruthy()
  })

  it("offers the ratio with no confirmation at all", () => {
    // The free path stays one click. A confirmation here would flatten the
    // distinction the locked panel below exists to draw.
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("free · reloads nothing")).toBeTruthy()
    expect(screen.getByRole("button", { name: /Apply ratio/ })).toBeTruthy()
  })

  it("locks the configuration fields until somebody asks for them", () => {
    // A stray keystroke must not reach a field that costs a weight reload.
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("locked")).toBeTruthy()
    expect(screen.queryByLabelText("Max batch tokens")).toBeNull()
    expect(
      screen.getByRole("button", { name: /Unlock to retune/ })
    ).toBeTruthy()
  })

  it("computes the cost from this workload's own counts", () => {
    // Two groups at 1:2 is six workers, and four decode at twenty minutes each
    // is the critical path. Not a fixed sentence about three workers.
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText(/all 6 workers reload/)).toBeTruthy()
    expect(screen.getByText(/1 h 20 min for the longest role/)).toBeTruthy()
  })

  it("says the model keeps serving when every role has a spare", () => {
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText(/The model keeps serving, slower/)).toBeTruthy()
  })

  it("says the model stops when a role has one worker", () => {
    // One group at 1:2 means a single prefill worker. Rolling it in place takes
    // prefill to zero, and a decode worker with no prefill peer serves nothing.
    const one = kimi({
      scale: {
        unit: "replicas",
        min: 1,
        max: 1,
        desired: 1,
        placed: 1,
        ready: 1,
      },
    })
    render(<RolesPanel workload={one} />)
    expect(
      screen.getByText(/The model stops serving for the whole reload/)
    ).toBeTruthy()
    expect(screen.getByText(/no prefill process at all/)).toBeTruthy()
  })

  it("warns when the ratio strands devices, without calling it an error", () => {
    // 24 does not divide 32: one group fits and a whole node sits idle. Nothing
    // refuses this anywhere, which is why the console has to say it.
    render(<RolesPanel workload={kimi()} budgetDevices={32} />)
    expect(
      screen.getByText(/1 unit fits and 8 devices would sit idle/)
    ).toBeTruthy()
  })

  it("says nothing about stranding when the group divides the budget", () => {
    render(<RolesPanel workload={kimi()} budgetDevices={48} />)
    expect(screen.queryByText(/sit idle/)).toBeNull()
  })

  it("says nothing about stranding with no budget to divide", () => {
    render(<RolesPanel workload={kimi()} />)
    expect(screen.queryByText(/sit idle/)).toBeNull()
  })

  it("reveals the fields only after the unlock, and puts the cost on the button", () => {
    // Unlocking is the intent. It is deliberate and recoverable, which is the
    // right size of gesture for an outage that is painful and survivable —
    // type-the-name is for losing data, and this loses none.
    return (async () => {
      render(<RolesPanel workload={kimi()} />)
      await userEvent.click(
        screen.getByRole("button", { name: /Unlock to retune/ })
      )
      expect(screen.getByText("unlocked")).toBeTruthy()
      expect(screen.getAllByLabelText("Max batch tokens").length).toBe(2)
      expect(
        screen.getByRole("button", { name: /Reload 6 workers · ~1 h 20 min/ })
      ).toBeTruthy()
    })()
  })

  it("re-locks and forgets the edit on cancel", () => {
    return (async () => {
      render(<RolesPanel workload={kimi()} />)
      await userEvent.click(
        screen.getByRole("button", { name: /Unlock to retune/ })
      )
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }))
      expect(screen.getByText("locked")).toBeTruthy()
      expect(screen.queryByLabelText("Max batch tokens")).toBeNull()
    })()
  })

  it("carries each role's whole configuration through a ratio change", async () => {
    // The roles block replaces whole and the plane reads every field off the
    // request, so weights alone would store a null batch size and an empty
    // argument map — a new digest, and a full reload of the two roles this
    // control promises not to touch.
    mutate.mockClear()
    render(<RolesPanel workload={kimi()} />)
    await userEvent.click(screen.getByRole("button", { name: "more decode" }))
    await userEvent.click(
      screen.getByRole("button", { name: /Apply 1 prefill : 3 decode/ })
    )
    expect(mutate).toHaveBeenCalledTimes(1)
    const body = mutate.mock.calls[0][0].body
    expect(body.serving.topology.roles).toEqual([
      {
        role: "prefill",
        weight: 1,
        max_batch_tokens: 32_768,
        engine_args: { "enforce-eager": "" },
      },
      { role: "decode", weight: 3, max_batch_tokens: 2048, engine_args: {} },
    ])
  })

  it("keeps the weights when only the configuration changes", async () => {
    // The mirror of the case above: the destructive path must not reset the
    // ratio somebody set from the panel above it.
    mutate.mockClear()
    render(<RolesPanel workload={kimi()} />)
    await userEvent.click(
      screen.getByRole("button", { name: /Unlock to retune/ })
    )
    await userEvent.click(
      screen.getByRole("button", { name: /Reload 6 workers/ })
    )
    expect(mutate).not.toHaveBeenCalled()
  })
})
