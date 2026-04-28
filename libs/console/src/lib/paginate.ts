import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import type { ListResponse } from "@/lib/api"

export type PageFetcher<T> = (cursor?: string) => Promise<ListResponse<T>>

/**
 * A cursor-following list. `count` on the envelope is the size of the page, not
 * of the collection, so the console follows `next_cursor` rather than rendering
 * the first page as though it were everything.
 */
export function useCursorList<T>(
  queryKey: readonly unknown[],
  fetchPage: PageFetcher<T>,
  options: {
    enabled?: boolean
    refetchInterval?: number
    /**
     * Poll only while the rows loaded so far say there is something to wait
     * for. A fixed interval keeps asking a question whose answer has stopped
     * changing; this stops on its own once nothing is in flight.
     */
    pollWhile?: (items: T[]) => boolean
    pollMs?: number
  } = {}
) {
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) => fetchPage(pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last: ListResponse<T>) => last.next_cursor ?? undefined,
    enabled: options.enabled,
    refetchInterval: options.pollWhile
      ? (q) => {
          const pages = q.state.data?.pages as ListResponse<T>[] | undefined
          const loaded = pages?.flatMap((p) => p.items) ?? []
          return options.pollWhile!(loaded) ? (options.pollMs ?? 5000) : false
        }
      : options.refetchInterval,
  })

  const items = query.data?.pages.flatMap((p) => p.items) ?? undefined

  return {
    items,
    /** How many rows have been loaded so far — never a collection total. */
    loaded: items?.length ?? 0,
    isLoading: query.isLoading,
    error: query.error,
    hasNextPage: query.hasNextPage,
    isFetchingNextPage: query.isFetchingNextPage,
    fetchNextPage: query.fetchNextPage,
    refetch: query.refetch,
  }
}

const MAX_PAGES = 40

/**
 * Drains every page. Reserved for the small, bounded collections a filter
 * control has to enumerate — clusters, projects, priority classes — and capped
 * so a runaway cursor cannot spin forever.
 */
export async function fetchAllPages<T>(
  fetchPage: PageFetcher<T>
): Promise<T[]> {
  const out: T[] = []
  let cursor: string | undefined
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await fetchPage(cursor)
    out.push(...page.items)
    if (!page.next_cursor) break
    cursor = page.next_cursor
  }
  return out
}

/** A drained collection as a query, for filter controls and lookup maps. */
export function useAllPages<T>(
  queryKey: readonly unknown[],
  fetchPage: PageFetcher<T>,
  options: { enabled?: boolean; refetchInterval?: number } = {}
) {
  return useQuery({
    queryKey: [...queryKey, "all"],
    queryFn: () => fetchAllPages(fetchPage),
    enabled: options.enabled,
    refetchInterval: options.refetchInterval,
  })
}
