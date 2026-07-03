import { render, screen } from "@/test/render"
import { describe, expect, it } from "vitest"

import { RolesPanel } from "./serving-topology"
import type { Workload } from "@/modules/demand/types"

/** Declared 1:2 over two groups; one prefill up against four decode. */
function kimi(over: Partial<Workload> = {}): Workload {
  return {
    id: "wkl_k",
    name: "kimi-k3-disagg",
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

describe("the roles panel", () => {
  it("names both ends of the declared ratio and what a group costs", () => {
    render(<RolesPanel workload={kimi()} />)
    expect(screen.getByText("1 prefill : 2 decode declared")).toBeTruthy()
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
})
