import {
  getAuthToken,
  getRefreshToken,
  reportUnauthorized,
} from "@/modules/auth/token"
import { applyRefreshedTokens } from "@/modules/auth/store"
import { config } from "@/lib/config"
import {
  ConflictError,
  ForbiddenError,
  InvalidError,
  NetworkError,
  NotAuthorizedError,
  NotFoundError,
  PlaneError,
  buildQueryString,
  type HttpMethod,
  type RequestOptions,
  type Transport,
} from "./transport"

/** Where the API lives; see {@link config}. Empty means the origin that served the console. */
export const BASE_URL = config.apiBaseUrl
export const API_PREFIX = "/v1"

interface WireError {
  code: string
  message: string
  detail: Record<string, unknown>
}

/**
 * Reads the plane's `{code, message, detail}` body. A body that is not that
 * shape (a proxy's HTML, say) degrades to the status line instead of
 * inventing a code the caller might branch on.
 */
function wireError(status: number, payload: unknown): WireError {
  const body = (payload ?? {}) as Partial<WireError>
  return {
    code: typeof body.code === "string" ? body.code : "http_error",
    message:
      typeof body.message === "string" && body.message !== ""
        ? body.message
        : `Request failed with status ${status}`,
    detail:
      body.detail && typeof body.detail === "object"
        ? (body.detail as Record<string, unknown>)
        : {},
  }
}

function toError(status: number, payload: unknown): PlaneError {
  const { code, message, detail } = wireError(status, payload)
  switch (status) {
    case 401:
      return new NotAuthorizedError(message, detail)
    case 403:
      return new ForbiddenError(message, detail)
    case 404:
      return new NotFoundError(message, detail)
    case 409:
      return new ConflictError(code, message, detail)
    case 422:
      return code === "invalid"
        ? new InvalidError(message, detail)
        : new PlaneError(status, code, message, detail)
    default:
      return new PlaneError(status, code, message, detail)
  }
}

interface RefreshResponse {
  access_token: string
  refresh_token: string
  expires_in: number
}

/**
 * Serializes concurrent 401s onto a single refresh attempt. Without this, five
 * in-flight requests that all fail simultaneously would each call the refresh
 * endpoint, and the plane's rotation would invalidate four of the five new
 * tokens before they were used.
 */
let refreshing: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (refreshing) return refreshing
  refreshing = doRefresh()
  try {
    return await refreshing
  } finally {
    refreshing = null
  }
}

async function doRefresh(): Promise<boolean> {
  const token = getRefreshToken()
  if (!token) return false
  try {
    const response = await fetch(`${BASE_URL}${API_PREFIX}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: token }),
    })
    if (!response.ok) return false
    const body = (await response.json()) as RefreshResponse
    applyRefreshedTokens(body.access_token, body.refresh_token)
    return true
  } catch {
    return false
  }
}

async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  return doRequest<T>(method, path, options, true)
}

async function doRequest<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions,
  canRefresh: boolean
): Promise<T> {
  const url = `${BASE_URL}${API_PREFIX}${path}${buildQueryString(options.query)}`
  const token = getAuthToken()

  let response: Response
  try {
    response = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.ifMatch ? { "If-Match": options.ifMatch } : {}),
        ...(options.idempotencyKey
          ? { "Idempotency-Key": options.idempotencyKey }
          : {}),
      },
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new NetworkError()
  }

  if (response.status === 204) return undefined as T

  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401) {
      if (canRefresh) {
        const refreshed = await tryRefresh()
        if (refreshed) {
          return doRequest<T>(method, path, options, false)
        }
      }
      reportUnauthorized()
    }
    throw toError(response.status, payload)
  }
  return payload as T
}

export const httpTransport: Transport = { kind: "http", request }
