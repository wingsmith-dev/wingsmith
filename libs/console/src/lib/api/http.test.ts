import { afterEach, describe, expect, it, vi } from "vitest"

import { httpTransport } from "./http"
import { ConflictError, InvalidError, PlaneError } from "./transport"

function answer(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status }))
  )
}

describe("httpTransport errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("raises InvalidError for the plane's 422 invalid", async () => {
    answer(422, { code: "invalid", message: "no", detail: { field: "x" } })
    const failure = httpTransport.request("POST", "/things", { body: {} })
    await expect(failure).rejects.toBeInstanceOf(InvalidError)
    await expect(failure).rejects.toMatchObject({
      status: 422,
      code: "invalid",
      detail: { field: "x" },
    })
  })

  it("keeps 409 conflict as ConflictError", async () => {
    answer(409, { code: "conflict", message: "stale", detail: {} })
    await expect(
      httpTransport.request("PATCH", "/things/a", { body: {} })
    ).rejects.toBeInstanceOf(ConflictError)
  })

  it("leaves a 422 with another code as a plain PlaneError", async () => {
    answer(422, { code: "something_else", message: "no", detail: {} })
    const failure = httpTransport.request("POST", "/things", { body: {} })
    await expect(failure).rejects.toBeInstanceOf(PlaneError)
    await expect(failure).rejects.not.toBeInstanceOf(InvalidError)
  })
})
