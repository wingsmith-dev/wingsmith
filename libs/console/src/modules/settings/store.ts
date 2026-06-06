import { create } from "zustand"
import { persist } from "zustand/middleware"

export const ACCENTS = [
  "cyan",
  "indigo",
  "cobalt",
  "violet",
  "magenta",
] as const

export type Accent = (typeof ACCENTS)[number]

interface SettingsState {
  /** Refuses every write from this browser. See `ReadOnlyError`. */
  readOnly: boolean
  setReadOnly: (readOnly: boolean) => void
  accent: Accent
  setAccent: (accent: Accent) => void
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
      accent: "cyan",
      setAccent: (accent) => {
        applyAccent(accent)
        set({ accent })
      },
    }),
    {
      name: "hangar.settings",
      onRehydrateStorage: () => (state) => applyAccent(state?.accent ?? "cyan"),
    }
  )
)

/**
 * Paints the accent kept in `localStorage` before the first render, so a reload
 * comes back in the operator's colour instead of the default and then switching.
 */
export function applyStoredAccent(): void {
  applyAccent(useSettingsStore.getState().accent)
}

/** The accent is a data attribute on the root, so it composes with `.dark`. */
function applyAccent(accent: Accent): void {
  document.documentElement.dataset.accent = accent
}

export function useAccent(): Accent {
  return useSettingsStore((s) => s.accent)
}

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
