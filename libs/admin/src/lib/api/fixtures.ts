import {
  NotFoundError,
  type HttpMethod,
  type RequestOptions,
  type Transport,
} from "./transport"

export interface FixtureContext {
  params: Record<string, string>
  query: NonNullable<RequestOptions["query"]>
  body: unknown
}

export interface FixtureRoute {
  method: HttpMethod
  /** Path template with `:name` segments, e.g. `/clusters/:clusterId/nodes`. */
  path: string
  handler: (ctx: FixtureContext) => unknown
}

const routes: FixtureRoute[] = []

export function registerFixtures(entries: FixtureRoute[]): void {
  routes.push(...entries)
}

const FIXTURE_LATENCY_MS = 220

function match(
  template: string,
  path: string
): Record<string, string> | null {
  const templateParts = template.split("/").filter(Boolean)
  const pathParts = path.split("/").filter(Boolean)
  if (templateParts.length !== pathParts.length) return null

  const params: Record<string, string> = {}
  for (let i = 0; i < templateParts.length; i++) {
    const t = templateParts[i]
    if (t.startsWith(":")) {
      params[t.slice(1)] = decodeURIComponent(pathParts[i])
      continue
    }
    if (t !== pathParts[i]) return null
  }
  return params
}

async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  await new Promise((resolve) => setTimeout(resolve, FIXTURE_LATENCY_MS))

  for (const route of routes) {
    if (route.method !== method) continue
    const params = match(route.path, path)
    if (!params) continue
    return route.handler({
      params,
      query: options.query ?? {},
      body: options.body,
    }) as T
  }

  throw new NotFoundError(`No fixture registered for ${method} ${path}`)
}

export const fixtureTransport: Transport = { kind: "fixtures", request }

/** Wraps fixture rows in the plane's list envelope. */
export function page<T>(items: T[]): {
  items: T[]
  count: number
  next_cursor: string | null
} {
  return { items, count: items.length, next_cursor: null }
}
