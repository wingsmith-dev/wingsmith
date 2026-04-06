import { create } from "zustand"
import { persist } from "zustand/middleware"
import { setAuthToken } from "./token"
import { BASE_URL, API_PREFIX } from "@/lib/api/http"

export interface Operator {
  id: string
  email: string
  name: string
}

/**
 * The plane issues one session token for one shared admin password
 * (`POST /v1/auth/login`), so there is no per-person identity to show yet. This
 * stands in for one in the chrome rather than pretending the token names a user.
 */
const ADMIN_OPERATOR: Operator = {
  id: "op_admin",
  email: "admin@hangar.local",
  name: "Admin",
}

interface LoginResponse {
  token: string
  expires_in: number
}

interface AuthState {
  operator: Operator | null
  accessToken: string | null
  isAuthenticated: boolean
  signIn: (password: string) => Promise<void>
  signOut: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      operator: null,
      accessToken: null,
      isAuthenticated: false,
      /**
       * Deliberately not routed through `httpTransport`: that attaches the bearer
       * token this call exists to obtain, and its fixtures mode would answer a
       * login the plane never saw.
       */
      signIn: async (password: string) => {
        const response = await fetch(`${BASE_URL}${API_PREFIX}/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password }),
        })
        if (!response.ok) {
          throw new Error(
            response.status === 401
              ? "Incorrect password."
              : `Sign in failed with status ${response.status}.`
          )
        }
        const { token } = (await response.json()) as LoginResponse
        setAuthToken(token)
        set({
          operator: ADMIN_OPERATOR,
          accessToken: token,
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
      // The token outlives a reload but not its own expiry, and nothing here
      // knows when that is. An expired one rehydrates, 401s, and is cleared by
      // signing out again — which is the same path a wrong password takes.
      onRehydrateStorage: () => (state) => {
        setAuthToken(state?.accessToken ?? null)
      },
    }
  )
)
