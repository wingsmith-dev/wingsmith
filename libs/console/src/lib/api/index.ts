import { isReadOnly } from "@/modules/settings/store"
import { httpTransport } from "./http"
import { ReadOnlyError } from "./transport"
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
 *
 * The mode is a build-time constant and the offline plane is reached only
 * through a dynamic import inside its branch, so a normal build drops both the
 * branch and the fixture corpus from the bundle.
 */
export const transport: Transport =
  import.meta.env.VITE_API_MODE === "fixtures"
    ? {
        kind: "fixtures",
        request: (method, path, options) =>
          import("@/modules/fixtures").then((offline) =>
            offline.fixtureTransport.request(method, path, options)
          ),
      }
    : httpTransport

function call<T>(method: HttpMethod, path: string, options?: RequestOptions) {
  if (method !== "GET" && isReadOnly())
    return Promise.reject(new ReadOnlyError())
  return transport.request<T>(method, path, options)
}

export const api = {
  get: <T>(path: string, query?: RequestOptions["query"]) =>
    call<T>("GET", path, { query }),
  list: <T>(path: string, query?: RequestOptions["query"]) =>
    call<ListResponse<T>>("GET", path, { query }),
  post: <T>(
    path: string,
    body?: unknown,
    options?: Omit<RequestOptions, "body">
  ) => call<T>("POST", path, { ...options, body }),
  /**
   * A `POST` that reads. `/placement/preview` and `/placement/expand` take a
   * body and write nothing, so they must survive read-only mode — which keys off
   * the method, because for every other route the method is the truth.
   *
   * Deliberately its own name rather than a flag on `post`: the guard exists to
   * make writing impossible from this browser, and the only safe way to punch
   * through it is one where every caller has to say out loud that it is reading.
   */
  query: <T>(path: string, body?: unknown) =>
    transport.request<T>("POST", path, { body }),
  patch: <T>(
    path: string,
    body?: unknown,
    options?: Omit<RequestOptions, "body">
  ) => call<T>("PATCH", path, { ...options, body }),
  put: <T>(
    path: string,
    body?: unknown,
    options?: Omit<RequestOptions, "body">
  ) => call<T>("PUT", path, { ...options, body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    call<T>("DELETE", path, options),
}

export {
  ConflictError,
  ForbiddenError,
  InvalidError,
  NetworkError,
  NotAuthorizedError,
  NotFoundError,
  PlaneError,
  ReadOnlyError,
} from "./transport"
export type {
  HttpMethod,
  ListResponse,
  PageParams,
  QueryValue,
  RequestOptions,
  Transport,
} from "./transport"
