import { buildBoard, liveAssignments } from "@/modules/board/build"
import * as make from "@/modules/board/fixture"
import { useBoardStore } from "@/modules/board/store"
import type { Board } from "@/modules/board/types"
import { fireEvent, render, screen } from "@/test/render"
import { beforeEach, describe, expect, it } from "vitest"
import { FleetBoard } from "./FleetBoard"

/** One cluster, one fabric, four devices, two of them held by `wkl_a`. */
function board(): Board {
  const cluster = make.cluster("iad-1", "us-east-1")
  const node = make.node("iad-1-n01", cluster.id)
  const fabric = make.domain("tpd_a", cluster.id, node.id)
  const devices = [0, 1, 2, 3].map((index) =>
    make.device(`d${index}`, {
      cluster: cluster.id,
      node: node.id,
      index,
      domain: fabric,
    })
  )
  const held = make.allocation("alc_a", {
    cluster: cluster.id,
    workload: "wkl_a",
    devices: [devices[0], devices[1]],
  })
  return buildBoard({
    clusters: [cluster],
    devices,
    domains: [fabric],
    nodes: [node],
    assignments: liveAssignments([held]),
  })
}

const cell = (index: number) =>
  screen.getByLabelText(new RegExp(`gpu${index} ·`))

function paint(mode: "live" | "sandbox") {
  render(
    <FleetBoard
      board={board()}
      isLoading={false}
      mode={mode}
      groupRegions={false}
    />
  )
}

describe("clicking a cell", () => {
  beforeEach(() => {
    useBoardStore.setState({
      selection: null,
      armed: null,
      holding: null,
      moves: [],
      collapsed: {},
      inspecting: null,
    })
  })

  it("takes the whole allocation first and the device second", () => {
    paint("live")

    fireEvent.click(cell(0))
    expect(useBoardStore.getState().selection).toEqual({
      kind: "allocation",
      allocation: "alc_a",
    })

    fireEvent.click(cell(0))
    expect(useBoardStore.getState().selection).toEqual({
      kind: "device",
      id: "d0",
    })
  })

  it("takes a free cell as the device it is", () => {
    paint("live")

    fireEvent.click(cell(2))
    expect(useBoardStore.getState().selection).toEqual({
      kind: "device",
      id: "d2",
    })
  })

  it("never puts a brick in a live hand", () => {
    paint("live")

    fireEvent.click(cell(0))
    expect(useBoardStore.getState().armed).toBeNull()
    expect(useBoardStore.getState().holding).toBeNull()
  })

  it("opens what is running on a double click", () => {
    paint("live")

    fireEvent.doubleClick(cell(1))
    expect(useBoardStore.getState().inspecting).toBe("alc_a")
  })
})

describe("moving what is running, in the sandbox", () => {
  beforeEach(() => {
    useBoardStore.setState({
      selection: null,
      armed: null,
      holding: null,
      moves: [],
      collapsed: {},
      inspecting: null,
    })
  })

  it("moves the allocation rather than copying it", () => {
    paint("sandbox")

    fireEvent.click(cell(0))
    expect(useBoardStore.getState().armed?.label).toBe("wkl_a")
    expect(useBoardStore.getState().holding?.allocation).toBe("alc_a")

    fireEvent.click(cell(2))
    const moves = useBoardStore.getState().moves
    expect(moves).toHaveLength(1)
    expect(moves[0].kind).toBe("move")
    if (moves[0].kind !== "move") throw new Error("expected a move")
    expect(moves[0].from).toEqual(["d0", "d1"])
    expect(moves[0].devices).toEqual(["d2", "d3"])
    expect(useBoardStore.getState().holding).toBeNull()
  })

  it("puts the brick back when the click lands inside it", () => {
    paint("sandbox")

    fireEvent.click(cell(0))
    fireEvent.click(cell(1))

    expect(useBoardStore.getState().moves).toHaveLength(0)
    expect(useBoardStore.getState().armed).toBeNull()
    expect(useBoardStore.getState().selection).toEqual({
      kind: "device",
      id: "d1",
    })
  })
})
