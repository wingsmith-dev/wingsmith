import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  getAuthToken,
  getRefreshToken,
  setAuthToken,
  setRefreshToken,
  setUnauthorizedHandler,
} from "@/modules/auth/token"
import { httpTransport } from "./http"
import {
  ConflictError,
  InvalidError,
  NetworkError,
  NotAuthorizedError,
  NotFoundError,
  PlaneError,
} from "./transport"

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

type Call = { url: string; init: RequestInit }

/** A fake plane: answers by URL suffix, records every call. */
function plane(handler: (call: Call) => Response | Promise<Response>): {
  calls: Call[]
} {
  const calls: Call[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const call = { url, init }
      calls.push(call)
      return handler(call)
    })
  )
  return { calls }
}

function bearer(call: Call): string | undefined {
  return (call.init.headers as Record<string, string>).Authorization
}

const isRefresh = (call: Call) => call.url.endsWith("/v1/auth/refresh")

beforeEach(() => {
  setAuthToken("old-access")
  setRefreshToken("old-refresh")
})

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
  setAuthToken(null)
  setRefreshToken(null)
})

describe("httpTransport", () => {
  it("sends the bearer token, the JSON body and the write headers", async () => {
    const { calls } = plane(() => json(201, { id: "wkl_1" }))

    const got = await httpTransport.request("POST", "/workloads", {
      body: { name: "a" },
      ifMatch: '"3"',
      idempotencyKey: "key-1",
      query: { project: "p" },
    })

    expect(got).toEqual({ id: "wkl_1" })
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toMatch(/\/v1\/workloads\?project=p$/)
    expect(calls[0].init.body).toBe(JSON.stringify({ name: "a" }))
    expect(calls[0].init.headers).toMatchObject({
      Authorization: "Bearer old-access",
      "If-Match": '"3"',
      "Idempotency-Key": "key-1",
    })
  })

  it("returns undefined for a 204", async () => {
    plane(() => new Response(null, { status: 204 }))
    await expect(
      httpTransport.request("DELETE", "/workloads/wkl_1")
    ).resolves.toBeUndefined()
  })

  it("refreshes once on a 401 and retries with the new token", async () => {
    const { calls } = plane((call) => {
      if (isRefresh(call))
        return json(200, {
          access_token: "new-access",
          refresh_token: "new-refresh",
          expires_in: 900,
        })
      return bearer(call) === "Bearer new-access"
        ? json(200, { ok: true })
        : json(401, { code: "unauthorized", message: "expired", detail: {} })
    })

    await expect(httpTransport.request("GET", "/clusters")).resolves.toEqual({
      ok: true,
    })

    expect(calls.filter(isRefresh)).toHaveLength(1)
    expect(JSON.parse(calls.find(isRefresh)!.init.body as string)).toEqual({
      refresh_token: "old-refresh",
    })
    expect(getAuthToken()).toBe("new-access")
    expect(getRefreshToken()).toBe("new-refresh")
  })

  it("shares one refresh between concurrent 401s", async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    const { calls } = plane(async (call) => {
      if (isRefresh(call)) {
        await gate
        return json(200, {
          access_token: "new-access",
          refresh_token: "new-refresh",
          expires_in: 900,
        })
      }
      return bearer(call) === "Bearer new-access"
        ? json(200, { ok: true })
        : json(401, { code: "unauthorized", message: "expired", detail: {} })
    })

    const pending = Promise.all([
      httpTransport.request("GET", "/clusters"),
      httpTransport.request("GET", "/nodes"),
      httpTransport.request("GET", "/devices"),
    ])
    await vi.waitFor(() => expect(calls.filter(isRefresh)).toHaveLength(1))
    release()

    await expect(pending).resolves.toHaveLength(3)
    expect(calls.filter(isRefresh)).toHaveLength(1)
  })

  it("reports the session expired when the refresh is refused", async () => {
    const expired = vi.fn()
    setUnauthorizedHandler(expired)
    plane((call) =>
      isRefresh(call)
        ? json(401, { code: "unauthorized", message: "revoked", detail: {} })
        : json(401, { code: "unauthorized", message: "expired", detail: {} })
    )

    await expect(httpTransport.request("GET", "/clusters")).rejects.toThrow(
      NotAuthorizedError
    )
    expect(expired).toHaveBeenCalledTimes(1)
  })

  it("does not retry a second time when the retried request is also refused", async () => {
    const { calls } = plane((call) =>
      isRefresh(call)
        ? json(200, {
            access_token: "new-access",
            refresh_token: "new-refresh",
            expires_in: 900,
          })
        : json(401, { code: "unauthorized", message: "nope", detail: {} })
    )

    await expect(httpTransport.request("GET", "/clusters")).rejects.toThrow(
      NotAuthorizedError
    )
    expect(calls.filter((call) => !isRefresh(call))).toHaveLength(2)
    expect(calls.filter(isRefresh)).toHaveLength(1)
  })

  it("skips the refresh when there is no refresh token", async () => {
    setRefreshToken(null)
    const { calls } = plane(() =>
      json(401, { code: "unauthorized", message: "expired", detail: {} })
    )

    await expect(httpTransport.request("GET", "/clusters")).rejects.toThrow(
      NotAuthorizedError
    )
    expect(calls).toHaveLength(1)
  })

  it("maps the plane's error envelope to typed errors", async () => {
    plane((call) =>
      call.url.includes("missing")
        ? json(404, { code: "not_found", message: "no such thing", detail: {} })
        : json(409, {
            code: "revision_conflict",
            message: "stale",
            detail: { current: 4 },
          })
    )

    await expect(
      httpTransport.request("GET", "/missing")
    ).rejects.toBeInstanceOf(NotFoundError)
    const conflict = await httpTransport
      .request("PATCH", "/workloads/wkl_1")
      .catch((error: unknown) => error)
    expect(conflict).toBeInstanceOf(ConflictError)
    expect(conflict).toMatchObject({
      code: "revision_conflict",
      message: "stale",
      detail: { current: 4 },
    })
  })

  it("degrades a body that is not the envelope to the status line", async () => {
    plane(
      () =>
        new Response("<html>bad gateway</html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        })
    )

    const error = await httpTransport
      .request("GET", "/clusters")
      .catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(PlaneError)
    expect(error).toMatchObject({
      status: 502,
      code: "http_error",
      message: "Request failed with status 502",
    })
  })

  it("turns a failed fetch into a NetworkError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch")
      })
    )
    await expect(httpTransport.request("GET", "/clusters")).rejects.toThrow(
      NetworkError
    )
  })
})

describe("httpTransport 422 and 409", () => {
  it("raises InvalidError for the plane's 422 invalid", async () => {
    plane(() =>
      json(422, { code: "invalid", message: "no", detail: { field: "x" } })
    )
    const failure = httpTransport.request("POST", "/things", { body: {} })
    await expect(failure).rejects.toBeInstanceOf(InvalidError)
    await expect(failure).rejects.toMatchObject({
      status: 422,
      code: "invalid",
      detail: { field: "x" },
    })
  })

  it("keeps 409 conflict as ConflictError", async () => {
    plane(() => json(409, { code: "conflict", message: "stale", detail: {} }))
    await expect(
      httpTransport.request("PATCH", "/things/a", { body: {} })
    ).rejects.toBeInstanceOf(ConflictError)
  })

  it("leaves a 422 with another code as a plain PlaneError", async () => {
    plane(() =>
      json(422, { code: "something_else", message: "no", detail: {} })
    )
    const failure = httpTransport.request("POST", "/things", { body: {} })
    await expect(failure).rejects.toBeInstanceOf(PlaneError)
    await expect(failure).rejects.not.toBeInstanceOf(InvalidError)
  })
})
