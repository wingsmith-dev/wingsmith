import { create } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"
import { setAuthToken, setRefreshToken, getRefreshToken } from "./token"
import { BASE_URL, API_PREFIX } from "@/lib/api/http"

export interface Operator {
  id: string
  email: string
  name: string
}

/**
 * Human access is a single operator: the plane issues one admin session for one
 * admin password (`POST /v1/auth/login`), so there is no per-person identity to
 * show. This names the role in the chrome rather than inventing a user.
 */
const ADMIN_OPERATOR: Operator = {
  id: "op_admin",
  email: "",
  name: "Admin",
}

interface LoginResponse {
  access_token: string
  refresh_token: string
  expires_in: number
  kind?: SessionKind
  tenant?: string | null
}

/**
 * Which application this session may see. `admin` is the console; `tenant` is the
 * serving view and nothing else.
 *
 * It is read off the login response and never off what was typed. A slug is a
 * claim until the credential behind it is verified, and the plane refuses a
 * tenant session on every other route regardless — this only decides what to
 * render, so that a tenant is not shown a navigation full of 401s.
 */
export type SessionKind = "admin" | "tenant"

interface AuthState {
  operator: Operator | null
  accessToken: string | null
  refreshToken: string | null
  isAuthenticated: boolean
  kind: SessionKind
  tenant: string | null
  signIn: (password: string, slug?: string) => Promise<void>
  signOut: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      operator: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      kind: "admin",
      tenant: null,
      /**
       * Deliberately not routed through `httpTransport`: that attaches the bearer
       * token this call exists to obtain, and its fixtures mode would answer a
       * login the plane never saw.
       */
      signIn: async (password: string, slug = "admin") => {
        // The offline plane has no auth endpoint, and a fixtures build exists to run with no
        // backend at all, so it signs in as the admin without asking anyone. Tenant sessions
        // need the plane to resolve the token and are refused here.
        if (import.meta.env.VITE_API_MODE === "fixtures") {
          if (slug !== "admin")
            throw new Error("Fixtures mode signs in as admin only.")
          set({
            operator: ADMIN_OPERATOR,
            accessToken: null,
            refreshToken: null,
            isAuthenticated: true,
            kind: "admin",
            tenant: null,
          })
          return
        }
        const response = await fetch(`${BASE_URL}${API_PREFIX}/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password, slug }),
        })
        if (!response.ok) {
          throw new Error(
            response.status === 401
              ? slug === "admin"
                ? "Incorrect password."
                : "That name and token do not match."
              : response.status === 429
                ? "Too many attempts. Try again shortly."
                : `Sign in failed with status ${response.status}.`
          )
        }
        const body = (await response.json()) as LoginResponse
        const kind = body.kind === "tenant" ? "tenant" : "admin"
        setAuthToken(body.access_token)
        setRefreshToken(body.refresh_token)
        set({
          operator:
            kind === "tenant"
              ? {
                  id: `tn_${body.tenant ?? slug}`,
                  email: "",
                  name: body.tenant ?? slug,
                }
              : ADMIN_OPERATOR,
          accessToken: body.access_token,
          refreshToken: body.refresh_token,
          isAuthenticated: true,
          kind,
          tenant: kind === "tenant" ? (body.tenant ?? slug) : null,
        })
      },
      signOut: () => {
        const refresh = get().refreshToken ?? getRefreshToken()
        if (refresh) {
          void fetch(`${BASE_URL}${API_PREFIX}/auth/logout`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ refresh_token: refresh }),
          }).catch(() => {})
        }
        setAuthToken(null)
        setRefreshToken(null)
        set({
          operator: null,
          accessToken: null,
          refreshToken: null,
          isAuthenticated: false,
          kind: "admin",
          tenant: null,
        })
      },
    }),
    {
      name: "hangar.auth",
      // Session storage, not local storage: the pair survives a reload but not
      // the tab, and is never shared with other tabs or left on disk after the
      // browser closes. A stolen refresh token mints sessions until it is
      // revoked, so it should live no longer than the page that uses it.
      storage: createJSONStorage(() => sessionStorage),
      // The access token outlives a reload but not its own expiry, and nothing
      // here knows when that is. An expired one rehydrates, the refresh token
      // is used to mint a new pair on the next 401, and only if that fails is
      // the session cleared by the handler installed in `session.ts`.
      onRehydrateStorage: () => (state) => {
        setAuthToken(state?.accessToken ?? null)
        setRefreshToken(state?.refreshToken ?? null)
      },
    }
  )
)

/**
 * The payload of a successful refresh. Called by the transport after it has
 * rotated the pair through `POST /v1/auth/refresh`.
 */
export function applyRefreshedTokens(
  accessToken: string,
  refreshToken: string
): void {
  setAuthToken(accessToken)
  setRefreshToken(refreshToken)
  useAuthStore.setState({ accessToken, refreshToken })
}
