import { useCallback } from "react"
import { useNavigate } from "@tanstack/react-router"

import { useAuthStore } from "./store"
import { setUnauthorizedHandler } from "./token"

/**
 * Signing out has to leave the page as well as clear the token. `/_app` guards
 * on `beforeLoad`, which only runs on navigation, so clearing the store while
 * standing on a page inside it leaves the operator looking at a screen they are
 * no longer allowed to load.
 */
export function useSignOut(): () => void {
  const signOut = useAuthStore((s) => s.signOut)
  const navigate = useNavigate()
  return useCallback(() => {
    signOut()
    void navigate({ to: "/login", replace: true })
  }, [signOut, navigate])
}

/**
 * A persisted token outlives the console but not its own expiry, and nothing on
 * this side knows when that is. Without this, an expired token rehydrates as a
 * signed-in session whose every request 401s, with no way back to the login
 * form — the state the operator has to be able to leave.
 */
export function installUnauthorizedHandler(redirect: () => void): () => void {
  setUnauthorizedHandler(() => {
    if (!useAuthStore.getState().isAuthenticated) return
    useAuthStore.getState().signOut()
    redirect()
  })
  return () => setUnauthorizedHandler(null)
}
