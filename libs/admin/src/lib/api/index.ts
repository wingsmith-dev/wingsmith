import "@/modules/fixtures"

import { fixtureTransport } from "./fixtures"
import { httpTransport } from "./http"
import type {
  HttpMethod,
  ListResponse,
  RequestOptions,
  Transport,
} from "./transport"

/**
 * The one-line seam. The real plane is the default; `VITE_API_MODE=fixtures`
 * opts into the offline stand-in. Defaulting the other way meant the HTTP path
 * was never exercised, and a transport nothing runs is a transport nothing
 * knows is broken.
 */
export const transport: Transport =
  import.meta.env.VITE_API_MODE === "fixtures" ? fixtureTransport : httpTransport

function call<T>(method: HttpMethod, path: string, options?: RequestOptions) {
  return transport.request<T>(method, path, options)
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"]) =>
    call<T>("GET", path, { query }),
  list: <T>(path: string, query?: RequestOptions["query"]) =>
    call<ListResponse<T>>("GET", path, { query }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "body">) =>
    call<T>("POST", path, { ...options, body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "body">) =>
    call<T>("PATCH", path, { ...options, body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "body">) =>
    call<T>("PUT", path, { ...options, body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    call<T>("DELETE", path, options),
}

export {
  ConflictError,
  ForbiddenError,
  NetworkError,
  NotAuthorizedError,
  NotFoundError,
  PlaneError,
} from "./transport"
export type {
  HttpMethod,
  ListResponse,
  PageParams,
  QueryValue,
  RequestOptions,
  Transport,
} from "./transport"
export {
  FixtureQuery,
  matchesLabels,
  matchesSearch,
  page,
  registerFixtures,
} from "./fixtures"
export type { FixtureContext, FixtureRoute } from "./fixtures"
