export type HttpMethod = "GET" | "POST" | "PATCH" | "PUT" | "DELETE"

/**
 * A query value. Arrays are emitted as repeated params, which is how the plane
 * accepts `label=k=v` more than once on one request.
 */
export type QueryValue = string | number | boolean | string[] | undefined | null

export interface RequestOptions {
  query?: Record<string, QueryValue>
  body?: unknown
  /** Conditional update against the object's `revision`. */
  ifMatch?: string
  /** Replay protection for mutating requests. */
  idempotencyKey?: string
}

/**
 * The single seam between the console and the Hangar plane. Every module talks
 * to the plane through this interface and nothing else, so the fixture and HTTP
 * implementations are interchangeable without touching a caller.
 */
export interface Transport {
  readonly kind: "http" | "fixtures"
  request<T>(
    method: HttpMethod,
    path: string,
    options?: RequestOptions
  ): Promise<T>
}

/**
 * The plane's error body, `{code, message, detail}`. `code` is the contract and
 * is what callers branch on; `message` is for people and is never parsed.
 */
export class PlaneError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    detail: Record<string, unknown> = {}
  ) {
    super(message)
    this.name = "PlaneError"
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export class NetworkError extends Error {
  constructor() {
    super(
      "Unable to reach the plane. The service may be temporarily unavailable."
    )
    this.name = "NetworkError"
  }
}

export class ForbiddenError extends PlaneError {
  constructor(message?: string, detail?: Record<string, unknown>) {
    super(
      403,
      "forbidden",
      message ?? "You do not have permission to perform this action.",
      detail
    )
    this.name = "ForbiddenError"
  }
}

export class NotAuthorizedError extends PlaneError {
  constructor(message?: string, detail?: Record<string, unknown>) {
    super(
      401,
      "not_authorized",
      message ?? "Your credentials were not accepted.",
      detail
    )
    this.name = "NotAuthorizedError"
  }
}

export class NotFoundError extends PlaneError {
  constructor(message?: string, detail?: Record<string, unknown>) {
    super(404, "not_found", message ?? "Not found.", detail)
    this.name = "NotFoundError"
  }
}

/** `409 conflict`: a failed `If-Match`, or a write that collides with current state. */
export class ConflictError extends PlaneError {
  constructor(code: string, message: string, detail?: Record<string, unknown>) {
    super(409, code, message, detail)
    this.name = "ConflictError"
  }
}

/** `422 invalid`: a well-formed request that asks for something the plane refuses. */
export class InvalidError extends PlaneError {
  constructor(message: string, detail?: Record<string, unknown>) {
    super(422, "invalid", message, detail)
    this.name = "InvalidError"
  }
}

/**
 * Raised before a request leaves the browser when the console is in read-only
 * mode. It is enforced here rather than only on the buttons, because a hidden
 * button is a suggestion and a guarded transport is a guarantee.
 */
export class ReadOnlyError extends PlaneError {
  constructor() {
    super(
      403,
      "read_only",
      "The console is in read-only mode. Turn it off in Settings to make changes."
    )
    this.name = "ReadOnlyError"
  }
}

export function buildQueryString(query: RequestOptions["query"]): string {
  if (!query) return ""
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue
    if (Array.isArray(value)) {
      for (const entry of value) if (entry !== "") params.append(key, entry)
      continue
    }
    params.append(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ""
}

/**
 * The plane's list envelope. Every collection endpoint returns this shape;
 * `next_cursor` is null when the page is the last one, and `count` is the size
 * of this page rather than the size of the collection.
 */
export interface ListResponse<T> {
  items: T[]
  count: number
  next_cursor: string | null
}
