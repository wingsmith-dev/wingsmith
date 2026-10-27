import {
  NotFoundError,
  type HttpMethod,
  type ListResponse,
  type RequestOptions,
  type Transport,
} from "./transport"

export interface FixtureContext {
  params: Record<string, string>
  query: FixtureQuery
  body: unknown
  options: RequestOptions
}

/**
 * A parsed query string. Repeated params keep every value, because `label` is
 * repeatable and a fixture that kept only the last one would answer a different
 * question from the plane.
 */
export class FixtureQuery {
  private readonly entries: [string, string][]

  constructor(query: RequestOptions["query"] = {}) {
    this.entries = []
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue
      if (Array.isArray(value)) {
        for (const entry of value)
          if (entry !== "") this.entries.push([key, entry])
        continue
      }
      this.entries.push([key, String(value)])
    }
  }

  get(key: string): string | undefined {
    return this.entries.find(([k]) => k === key)?.[1]
  }

  all(key: string): string[] {
    return this.entries.filter(([k]) => k === key).map(([, v]) => v)
  }

  int(key: string): number | undefined {
    const raw = this.get(key)
    if (raw === undefined) return undefined
    const parsed = Number.parseInt(raw, 10)
    return Number.isNaN(parsed) ? undefined : parsed
  }

  bool(key: string): boolean | undefined {
    const raw = this.get(key)
    if (raw === undefined) return undefined
    return raw === "true"
  }

  /** The `label=k=v` selector, parsed the way `app/core/filters.py` parses it. */
  labels(): Record<string, string> {
    const selector: Record<string, string> = {}
    for (const item of this.all("label")) {
      const index = item.indexOf("=")
      if (index <= 0) continue
      selector[item.slice(0, index)] = item.slice(index + 1)
    }
    return selector
  }
}

export interface FixtureRoute {
  method: HttpMethod
  /** Path template with `:name` segments, e.g. `/clusters/:ref`. */
  path: string
  handler: (ctx: FixtureContext) => unknown
}

const routes: FixtureRoute[] = []

export function registerFixtures(entries: FixtureRoute[]): void {
  routes.push(...entries)
}

const FIXTURE_LATENCY_MS = 180

function match(template: string, path: string): Record<string, string> | null {
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
      query: new FixtureQuery(options.query),
      body: options.body,
      options,
    }) as T
  }

  throw new NotFoundError(`No fixture registered for ${method} ${path}`)
}

export const fixtureTransport: Transport = { kind: "fixtures", request }

const DEFAULT_LIMIT = 50
const MAX_LIMIT = 200

/**
 * Slices fixture rows into the plane's list envelope, honoring `limit` and
 * `cursor` the way `app/core/pagination.py` does, including the opaque,
 * base64 cursor, so a client that hand-decodes one is as wrong here as it is
 * against the real thing.
 */
export function page<T>(rows: T[], query?: FixtureQuery): ListResponse<T> {
  const requested = query?.int("limit")
  const limit = Math.min(
    requested && requested > 0 ? requested : DEFAULT_LIMIT,
    MAX_LIMIT
  )
  const cursor = query?.get("cursor")
  const offset = cursor ? Number.parseInt(atob(pad(cursor)), 10) : 0
  const start = Number.isNaN(offset) ? 0 : offset

  const items = rows.slice(start, start + limit)
  const next = start + limit < rows.length ? encode(start + limit) : null
  return { items, count: items.length, next_cursor: next }
}

function encode(offset: number): string {
  return btoa(String(offset)).replace(/=+$/, "")
}

function pad(cursor: string): string {
  return cursor + "=".repeat((4 - (cursor.length % 4)) % 4)
}

/** Case-insensitive substring match, the shape every `?search=` filter takes. */
export function matchesSearch(
  needle: string | undefined,
  ...haystack: (string | null)[]
) {
  if (!needle) return true
  const term = needle.toLowerCase()
  return haystack.some((value) => value?.toLowerCase().includes(term))
}

/** Whether an object's labels satisfy a `label=k=v` selector. */
export function matchesLabels(
  labels: Record<string, string>,
  selector: Record<string, string>
): boolean {
  return Object.entries(selector).every(([key, value]) => labels[key] === value)
}
