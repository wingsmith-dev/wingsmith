import "@/modules/fixtures"

import { fixtureTransport } from "./fixtures"
import { httpTransport } from "./http"
import type { HttpMethod, RequestOptions, Transport } from "./transport"

/**
 * The one-line seam. Set `VITE_API_MODE=http` (or edit this line) to point the
 * whole console at the real plane; nothing else in the codebase changes.
 */
export const transport: Transport =
  import.meta.env.VITE_API_MODE === "http" ? httpTransport : fixtureTransport

function call<T>(method: HttpMethod, path: string, options?: RequestOptions) {
  return transport.request<T>(method, path, options)
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"]) =>
    call<T>("GET", path, { query }),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, { body }),
  patch: <T>(path: string, body?: unknown) => call<T>("PATCH", path, { body }),
  put: <T>(path: string, body?: unknown) => call<T>("PUT", path, { body }),
  delete: <T>(path: string) => call<T>("DELETE", path),
}

export {
  ApiError,
  ForbiddenError,
  NetworkError,
  NotFoundError,
} from "./transport"
export type {
  HttpMethod,
  ListResponse,
  RequestOptions,
  Transport,
} from "./transport"
export { page, registerFixtures } from "./fixtures"
export type { FixtureContext, FixtureRoute } from "./fixtures"
