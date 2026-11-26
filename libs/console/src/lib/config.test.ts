import { describe, expect, it } from "vitest"

import { resolveConfig } from "./config"

describe("resolveConfig", () => {
  it("defaults to the same origin and writable when nothing is set", () => {
    expect(resolveConfig(undefined, {})).toEqual({
      apiBaseUrl: "",
      readOnly: false,
      demoPassword: "",
    })
  })

  it("prefers the runtime values over the build-time ones", () => {
    expect(
      resolveConfig(
        { apiBaseUrl: "https://plane.example.com", readOnly: true },
        { VITE_API_BASE_URL: "http://localhost:8000", VITE_READ_ONLY: "false" }
      )
    ).toEqual({
      apiBaseUrl: "https://plane.example.com",
      readOnly: true,
      demoPassword: "",
    })
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
    ).toEqual({
      apiBaseUrl: "http://localhost:8000",
      readOnly: true,
      demoPassword: "",
    })
  })

  it("ignores values of the wrong type", () => {
    expect(resolveConfig({ apiBaseUrl: 42, readOnly: "maybe" }, {})).toEqual({
      apiBaseUrl: "",
      readOnly: false,
      demoPassword: "",
    })
  })

  it("reads the demo password from runtime config, then the build-time value", () => {
    expect(
      resolveConfig(
        { demoPassword: "runtime-pw" },
        { VITE_DEMO_PASSWORD: "build-pw" }
      ).demoPassword
    ).toBe("runtime-pw")
    expect(
      resolveConfig({}, { VITE_DEMO_PASSWORD: "build-pw" }).demoPassword
    ).toBe("build-pw")
  })
})
