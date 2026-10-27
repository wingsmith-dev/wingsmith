import {
  matchesLabels,
  matchesSearch,
  page,
  registerFixtures,
  type FixtureContext,
  type FixtureQuery,
} from "@/lib/api/fixtures"
import { NotFoundError } from "@/lib/api/transport"

/** Every object the plane can return has at least an id. */
interface Row {
  id: string
}

interface CollectionOptions<T extends Row> {
  path: string
  rows: () => T[]
  /** Names a row answers to besides its id: `libs/packages/contract/API.md` §3. */
  names?: (row: T) => (string | null | undefined)[]
  filter?: (row: T, query: FixtureQuery) => boolean
  /** What `?search=` matches against. */
  searchable?: (row: T) => (string | null)[]
  labels?: (row: T) => Record<string, string>
  /** Observed collections answer 405 to a write, the way the plane's do. */
  readOnly?: boolean
  singular: string
}

function matches<T extends Row>(
  row: T,
  query: FixtureQuery,
  opts: CollectionOptions<T>
) {
  if (opts.filter && !opts.filter(row, query)) return false
  if (
    opts.searchable &&
    !matchesSearch(query.get("search"), ...opts.searchable(row))
  ) {
    return false
  }
  if (opts.labels && !matchesLabels(opts.labels(row), query.labels()))
    return false
  return true
}

/**
 * Registers `GET /{path}` and `GET /{path}/{ref}` for one collection, with the
 * same envelope, cursor and reference semantics the plane has.
 */
export function collection<T extends Row>(opts: CollectionOptions<T>) {
  const resolve = (ref: string): T => {
    const found = opts
      .rows()
      .find(
        (row) =>
          row.id === ref ||
          (opts.names?.(row) ?? []).some((name) => name != null && name === ref)
      )
    if (!found) throw new NotFoundError(`no ${opts.singular} '${ref}'`)
    return found
  }

  registerFixtures([
    {
      method: "GET",
      path: opts.path,
      handler: ({ query }: FixtureContext) =>
        page(
          opts.rows().filter((row) => matches(row, query, opts)),
          query
        ),
    },
    {
      method: "GET",
      path: `${opts.path}/:ref`,
      handler: ({ params }: FixtureContext) => resolve(params.ref),
    },
  ])

  return { resolve }
}
