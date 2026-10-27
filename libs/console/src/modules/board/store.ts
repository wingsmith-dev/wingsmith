import { create } from "zustand"
import { moveId } from "./sandbox"
import type { Brick, Holder, Move, Selection } from "./types"

/** What is under the pointer mid-drag. Bricks come from the tray, allocations off the board. */
export type Drag =
  | { kind: "brick"; brick: Brick }
  | { kind: "allocation"; allocation: string; holder: Holder; from: string[] }

/**
 * Where the brick in hand came off the board, when it came off the board.
 *
 * A tray brick and a picked-up allocation are the same shape to every fit check,
 * and this is the one thing that separates them: dropping a tray brick adds a
 * workload, dropping this one moves the workload that is already there. Without
 * it a click-to-move leaves the devices behind still held and the plan gains a
 * second copy of something the fleet has one of.
 */
export interface Hand {
  allocation: string
  holder: Holder
  from: string[]
}

interface BoardState {
  /** The plan, in the order it was built. Replayed on every repaint. */
  moves: Move[]
  /** The brick armed for a click-to-place: off the tray, or off the board. */
  armed: Brick | null
  /** Set when `armed` was picked off the board. Null when it came from the tray. */
  holding: Hand | null
  /** The shape somebody typed, offered in the tray beside the real workloads. */
  custom: Brick | null
  selection: Selection
  dragging: Drag | null
  /** A workload to pick out of the board, by uid. Hover on the legend sets it. */
  highlight: string | null
  /** The allocation the detail dialog is open on, if any. */
  inspecting: string | null
  /** Collapsed regions, clusters and fabrics, by their board key. */
  collapsed: Record<string, boolean>

  arm: (brick: Brick | null) => void
  /** Take a live allocation into the hand, so the next drop is a move. */
  pickUp: (brick: Brick, hand: Hand) => void
  inspect: (allocation: string | null) => void
  setCustom: (brick: Brick | null) => void
  select: (selection: Selection) => void
  highlightWorkload: (workload: string | null) => void
  drag: (dragging: Drag | null) => void
  toggle: (key: string) => void
  setCollapsed: (key: string, collapsed: boolean) => void
  collapseAll: (keys: string[], collapsed: boolean) => void

  place: (brick: Brick, devices: string[]) => void
  placeMany: (
    entries: { brick: Brick; devices: string[]; replica: number }[]
  ) => void
  relocate: (
    allocation: string,
    holder: Holder,
    from: string[],
    devices: string[]
  ) => void
  release: (allocation: string, holder: Holder, from: string[]) => void
  undo: () => void
  clear: () => void
}

/**
 * The sandbox, and only the sandbox.
 *
 * Nothing in here is declared state and nothing in here is ever sent: a plan is
 * a client-side overlay on observed allocations, thrown away on reload. Keeping
 * it out of the URL is deliberate: a half-built plan in a shared link would
 * read as something the fleet was about to do.
 */
export const useBoardStore = create<BoardState>()((set) => ({
  moves: [],
  armed: null,
  holding: null,
  custom: null,
  selection: null,
  dragging: null,
  highlight: null,
  inspecting: null,
  collapsed: {},

  arm: (armed) => set({ armed, holding: null }),
  pickUp: (armed, holding) => set({ armed, holding }),
  inspect: (inspecting) => set({ inspecting }),
  setCustom: (custom) => set({ custom }),
  select: (selection) => set({ selection }),
  highlightWorkload: (highlight) => set({ highlight }),
  drag: (dragging) => set({ dragging }),
  toggle: (key) =>
    set((state) => ({
      collapsed: { ...state.collapsed, [key]: !state.collapsed[key] },
    })),
  setCollapsed: (key, collapsed) =>
    set((state) => ({ collapsed: { ...state.collapsed, [key]: collapsed } })),
  collapseAll: (keys, collapsed) =>
    set((state) => ({
      collapsed: {
        ...state.collapsed,
        ...Object.fromEntries(keys.map((key) => [key, collapsed])),
      },
    })),

  place: (brick, devices) =>
    set((state) => ({
      moves: [
        ...state.moves,
        { id: moveId("plan"), kind: "place", brick, devices },
      ],
      armed: null,
      holding: null,
    })),

  /**
   * A planned workload's replicas, added as one step so undo takes the plan back
   * out the way it went in. Placing six replicas is one decision, and six undos
   * to reverse it would be six.
   */
  placeMany: (entries) =>
    set((state) => {
      const batch = moveId("batch")
      return {
        moves: [
          ...state.moves,
          ...entries.map((entry) => ({
            id: moveId("plan"),
            kind: "place" as const,
            brick: entry.brick,
            devices: entry.devices,
            replica: entry.replica,
            batch,
          })),
        ],
        armed: null,
        holding: null,
      }
    }),

  /**
   * Relocating the same allocation twice replaces the earlier move instead of
   * stacking on it: the plan is a set of destinations, not a route, and two
   * moves of one allocation would free the devices it never held.
   */
  relocate: (allocation, holder, from, devices) =>
    set((state) => ({
      moves: [
        ...state.moves.filter((move) => idOf(move) !== allocation),
        { id: moveId("plan"), kind: "move", allocation, holder, from, devices },
      ],
      armed: null,
      holding: null,
    })),

  release: (allocation, holder, from) =>
    set((state) => ({
      moves: [
        ...state.moves.filter((move) => idOf(move) !== allocation),
        { id: moveId("plan"), kind: "release", allocation, holder, from },
      ],
    })),

  undo: () =>
    set((state) => {
      const last = state.moves[state.moves.length - 1]
      const batch = last?.kind === "place" ? last.batch : undefined
      if (!batch) return { moves: state.moves.slice(0, -1) }
      return {
        moves: state.moves.filter(
          (move) => !(move.kind === "place" && move.batch === batch)
        ),
      }
    }),
  clear: () => set({ moves: [], armed: null, holding: null, dragging: null }),
}))

function idOf(move: Move): string {
  return move.kind === "place" ? move.id : move.allocation
}
