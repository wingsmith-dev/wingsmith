import { getAuthToken } from "@/modules/auth/token"
import {
  ApiError,
  ForbiddenError,
  NetworkError,
  NotFoundError,
  buildQueryString,
  type HttpMethod,
  type RequestOptions,
  type Transport,
} from "./transport"

export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"
export const API_PREFIX = "/v1"

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
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new NetworkError()
  }

  if (response.status === 204) return undefined as T

  const payload = await response.json().catch(() => null)

  if (!response.ok) {
    const message =
      (payload && typeof payload === "object" && "message" in payload
        ? String((payload as { message: unknown }).message)
        : null) ?? `Request failed with status ${response.status}`
    if (response.status === 403) throw new ForbiddenError(message)
    if (response.status === 404) throw new NotFoundError(message)
    throw new ApiError(response.status, message)
  }

  return payload as T
}

export const httpTransport: Transport = { kind: "http", request }
