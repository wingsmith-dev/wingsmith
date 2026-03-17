export type HttpMethod = "GET" | "POST" | "PATCH" | "PUT" | "DELETE"

export interface RequestOptions {
  query?: Record<string, string | number | boolean | undefined>
  body?: unknown
}

/**
 * The single seam between the console and the Hangar plane. Every module talks
 * to the plane through this interface and nothing else, so the fixture and HTTP
 * implementations are interchangeable without touching a caller.
 */
export interface Transport {
  readonly kind: "http" | "fixtures"
  request<T>(method: HttpMethod, path: string, options?: RequestOptions): Promise<T>
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

export class NetworkError extends Error {
  constructor() {
    super("Unable to reach the plane. The service may be temporarily unavailable.")
    this.name = "NetworkError"
  }
}

export class ForbiddenError extends ApiError {
  constructor(message?: string) {
    super(403, message ?? "You do not have permission to perform this action.")
    this.name = "ForbiddenError"
  }
}

export class NotFoundError extends ApiError {
  constructor(message?: string) {
    super(404, message ?? "Not found.")
    this.name = "NotFoundError"
  }
}

export function buildQueryString(query: RequestOptions["query"]): string {
  if (!query) return ""
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === "") continue
    params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ""
}

/**
 * The plane's list envelope. Every collection endpoint returns this shape;
 * `next_cursor` is null when the page is the last one.
 */
export interface ListResponse<T> {
  items: T[]
  count: number
  next_cursor: string | null
}
