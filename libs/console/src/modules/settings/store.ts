import { create } from "zustand"
import { persist } from "zustand/middleware"

interface SettingsState {
  /** Refuses every write from this browser. See `ReadOnlyError`. */
  readOnly: boolean
  setReadOnly: (readOnly: boolean) => void
  /** Shows the unit-economics view. Off by default; see `useEconomics`. */
  economics: boolean
  setEconomics: (economics: boolean) => void
}

/**
 * Read-only is a property of this console session, not of the plane. It stops
 * this browser from writing; it does not stop anyone else, and it does not stop
 * the hub from reconciling a cluster toward declared state. Connecting a live
 * cluster safely is a separate, per-cluster decision.
 */
export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      readOnly: import.meta.env.VITE_READ_ONLY === "true",
      setReadOnly: (readOnly) => set({ readOnly }),
      economics: import.meta.env.VITE_ECONOMICS === "true",
      setEconomics: (economics) => set({ economics }),
    }),
    { name: "hangar.settings" }
  )
)

export function useReadOnly(): boolean {
  return useSettingsStore((s) => s.readOnly)
}

export function isReadOnly(): boolean {
  return useSettingsStore.getState().readOnly
}

/**
 * Whether this browser may see what the fleet costs and earns.
 *
 * Off unless `VITE_ECONOMICS` says otherwise, so a build with nothing set is
 * safe to put in front of a customer: the tab is not rendered, not disabled.
 * This hides a view; it is not a permission, and it protects nobody from a
 * console they can rebuild. The numbers it shows are the operator's own model
 * and never came from the plane, which is why hiding them is a display
 * decision and can live in this browser.
 */
export function useEconomics(): boolean {
  return useSettingsStore((s) => s.economics)
}
