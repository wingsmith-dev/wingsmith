import { afterEach, describe, expect, it, vi } from "vitest"
import { useAuthStore } from "./store"

afterEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null)))
  useAuthStore.getState().signOut()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe("signIn", () => {
  it("signs in as admin without calling the plane in fixtures mode", async () => {
    vi.stubEnv("VITE_API_MODE", "fixtures")
    const fetch = vi.fn()
    vi.stubGlobal("fetch", fetch)

    await useAuthStore.getState().signIn("anything")

    expect(fetch).not.toHaveBeenCalled()
    expect(useAuthStore.getState()).toMatchObject({
      isAuthenticated: true,
      kind: "admin",
    })
  })

  it("refuses a tenant sign-in in fixtures mode", async () => {
    vi.stubEnv("VITE_API_MODE", "fixtures")
    await expect(
      useAuthStore.getState().signIn("hgrt1.token", "acme")
    ).rejects.toThrow("admin only")
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
  })

  it("posts the password to the plane otherwise", async () => {
    vi.stubEnv("VITE_API_MODE", "http")
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: "a",
          refresh_token: "r",
          expires_in: 60,
          kind: "admin",
        }),
        { status: 200 }
      )
    )
    vi.stubGlobal("fetch", fetch)

    await useAuthStore.getState().signIn("secret")

    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/v1\/auth\/login$/),
      expect.objectContaining({ method: "POST" })
    )
    expect(useAuthStore.getState().refreshToken).toBe("r")
  })
})
