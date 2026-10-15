import { describe, expect, it } from "vitest"

import { resolveConfig } from "./config"

describe("resolveConfig", () => {
  it("defaults to the same origin and writable when nothing is set", () => {
    expect(resolveConfig(undefined, {})).toEqual({
      apiBaseUrl: "",
      readOnly: false,
    })
  })

  it("prefers the runtime values over the build-time ones", () => {
    expect(
      resolveConfig(
        { apiBaseUrl: "https://plane.example.com", readOnly: true },
        { VITE_API_BASE_URL: "http://localhost:8000", VITE_READ_ONLY: "false" }
      )
    ).toEqual({ apiBaseUrl: "https://plane.example.com", readOnly: true })
  })

  it("treats an empty runtime base URL as same origin, not as unset", () => {
    expect(
      resolveConfig(
        { apiBaseUrl: "" },
        { VITE_API_BASE_URL: "http://localhost:8000" }
      ).apiBaseUrl
    ).toBe("")
  })

  it("falls back to the build-time values when config.js sets nothing", () => {
    expect(
      resolveConfig(
        {},
        { VITE_API_BASE_URL: "http://localhost:8000/", VITE_READ_ONLY: "true" }
      )
    ).toEqual({ apiBaseUrl: "http://localhost:8000", readOnly: true })
  })

  it("ignores values of the wrong type", () => {
    expect(resolveConfig({ apiBaseUrl: 42, readOnly: "maybe" }, {})).toEqual({
      apiBaseUrl: "",
      readOnly: false,
    })
  })
})
