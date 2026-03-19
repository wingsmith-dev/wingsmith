import { create } from "zustand"
import { persist } from "zustand/middleware"

export const ACCENTS = ["cyan", "indigo", "cobalt", "violet", "magenta"] as const

export type Accent = (typeof ACCENTS)[number]

interface SettingsState {
  /** Refuses every write from this browser. See `ReadOnlyError`. */
  readOnly: boolean
  setReadOnly: (readOnly: boolean) => void
  accent: Accent
  setAccent: (accent: Accent) => void
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
