import { create } from "zustand"
import { persist } from "zustand/middleware"
import { setAuthToken } from "./token"

export interface Operator {
  id: string
  email: string
  name: string
}

/** Stubbed identity. Replaced wholesale when real auth lands. */
const STUB_TOKEN = import.meta.env.VITE_API_TOKEN ?? "hangar-dev-static-token"

const STUB_OPERATOR: Operator = {
  id: "op_stub",
  email: "operator@hangar.local",
  name: "Operator",
}

interface AuthState {
  operator: Operator | null
  accessToken: string | null
  isAuthenticated: boolean
  signIn: () => void
  signOut: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      operator: null,
      accessToken: null,
      isAuthenticated: false,
      signIn: () => {
        setAuthToken(STUB_TOKEN)
        set({
          operator: STUB_OPERATOR,
          accessToken: STUB_TOKEN,
          isAuthenticated: true,
        })
      },
      signOut: () => {
        setAuthToken(null)
        set({ operator: null, accessToken: null, isAuthenticated: false })
      },
    }),
    {
      name: "hangar.auth",
      onRehydrateStorage: () => (state) => {
        setAuthToken(state?.accessToken ?? null)
      },
    }
  )
)
