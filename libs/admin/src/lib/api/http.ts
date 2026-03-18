import { getAuthToken } from "@/modules/auth/token"
import {
  ConflictError,
  ForbiddenError,
  NetworkError,
  NotAuthorizedError,
  NotFoundError,
  PlaneError,
  buildQueryString,
  type HttpMethod,
  type RequestOptions,
  type Transport,
} from "./transport"

export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"
export const API_PREFIX = "/v1"

/** Scopes every request to one project, per `contract/API.md` §2. */
const PROJECT = import.meta.env.VITE_API_PROJECT as string | undefined

interface WireError {
  code: string
  message: string
  detail: Record<string, unknown>
}

/**
 * Reads the plane's `{code, message, detail}` body. A body that is not that
 * shape — a proxy's HTML, say — degrades to the status line rather than
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
    default:
      return new PlaneError(status, code, message, detail)
  }
}

async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {}
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
        ...(PROJECT ? { "X-Hangar-Project": PROJECT } : {}),
        ...(options.ifMatch ? { "If-Match": options.ifMatch } : {}),
        ...(options.idempotencyKey
          ? { "Idempotency-Key": options.idempotencyKey }
          : {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new NetworkError()
  }

  if (response.status === 204) return undefined as T

  const payload = await response.json().catch(() => null)
  if (!response.ok) throw toError(response.status, payload)
  return payload as T
}

export const httpTransport: Transport = { kind: "http", request }
