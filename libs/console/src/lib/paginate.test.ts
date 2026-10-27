import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { createElement } from "react"
import { describe, expect, it, vi } from "vitest"
import type { ListResponse } from "@/lib/api"
import {
  MAX_PAGES,
  anyTruncated,
  fetchAllPages,
  useAllPages,
  useCursorList,
} from "./paginate"

type Row = { id: string }

/** A fetcher that serves the pages it was given, in order, keyed by cursor. */
function pager(pages: ListResponse<Row>[]) {
  const seen: (string | undefined)[] = []
  const fetch = vi.fn(async (cursor?: string) => {
    seen.push(cursor)
    const index = cursor ? Number(cursor) : 0
    return pages[index]
  })
  return { fetch, seen }
}

function page(ids: string[], next: string | null): ListResponse<Row> {
  return {
    items: ids.map((id) => ({ id })),
    count: ids.length,
    next_cursor: next,
  }
}

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
}

describe("fetchAllPages", () => {
  it("follows the cursor to the end", async () => {
    const { fetch, seen } = pager([
      page(["a"], "1"),
      page(["b"], "2"),
      page(["c"], null),
    ])
    const got = await fetchAllPages(fetch)

    expect(got.map((r) => r.id)).toEqual(["a", "b", "c"])
    expect(seen).toEqual([undefined, "1", "2"])
  })

  it("stops at the first page when there is no cursor", async () => {
    const { fetch } = pager([page(["a"], null)])
    expect(await fetchAllPages(fetch)).toHaveLength(1)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("handles an empty collection", async () => {
    const { fetch } = pager([page([], null)])
    expect([...(await fetchAllPages(fetch))]).toEqual([])
  })

  it("caps itself so a runaway cursor cannot spin forever", async () => {
    // A server that always returns a cursor would otherwise hang the control that called it.
    const fetch = vi.fn(async () => page(["x"], "always"))
    const got = await fetchAllPages(fetch)

    expect(fetch.mock.calls.length).toBe(MAX_PAGES)
    expect(got.length).toBe(fetch.mock.calls.length)
    expect(got.truncated).toBe(true)
  })

  it("is not truncated when the cursor runs out", async () => {
    const { fetch } = pager([page(["a"], "1"), page(["b"], null)])
    expect((await fetchAllPages(fetch)).truncated).toBe(false)
  })

  it("is not truncated when the last allowed page has no cursor", async () => {
    const pages = Array.from({ length: MAX_PAGES }, (_, i) =>
      page([String(i)], i < MAX_PAGES - 1 ? String(i + 1) : null)
    )
    const { fetch } = pager(pages)
    const got = await fetchAllPages(fetch)
    expect(got).toHaveLength(MAX_PAGES)
    expect(got.truncated).toBe(false)
  })

  it("lets a failure surface instead of returning a short list", async () => {
    // Half a collection silently returned would make a filter control omit real options.
    const fetch = vi
      .fn<(cursor?: string) => Promise<ListResponse<Row>>>()
      .mockResolvedValueOnce(page(["a"], "1"))
      .mockRejectedValueOnce(new Error("the plane is unreachable"))

    await expect(fetchAllPages(fetch)).rejects.toThrow("unreachable")
  })
})

describe("useCursorList", () => {
  it("exposes the first page and reports that there is more", async () => {
    const { fetch } = pager([page(["a"], "1"), page(["b"], null)])
    const { result } = renderHook(() => useCursorList(["rows"], fetch), {
      wrapper: wrapper(),
    })

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.items?.map((r) => r.id)).toEqual(["a"])
    expect(result.current.hasNextPage).toBe(true)
    // `count` on the envelope is the size of the page, not of the collection.
    expect(result.current.loaded).toBe(1)
  })

  it("appends the next page instead of replacing the first", async () => {
    const { fetch } = pager([page(["a"], "1"), page(["b"], null)])
    const { result } = renderHook(() => useCursorList(["rows"], fetch), {
      wrapper: wrapper(),
    })

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    await result.current.fetchNextPage()

    await waitFor(() =>
      expect(result.current.items?.map((r) => r.id)).toEqual(["a", "b"])
    )
    expect(result.current.hasNextPage).toBe(false)
    expect(result.current.loaded).toBe(2)
  })

  it("is undefined before it has loaded, so a table can tell empty from unloaded", async () => {
    const { fetch } = pager([page([], null)])
    const { result } = renderHook(() => useCursorList(["rows"], fetch), {
      wrapper: wrapper(),
    })

    expect(result.current.items).toBeUndefined()
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.items).toEqual([])
  })

  it("surfaces an error instead of an empty table", async () => {
    const fetch = vi.fn(async () => {
      throw new Error("the plane is unreachable")
    })
    const { result } = renderHook(() => useCursorList(["rows"], fetch), {
      wrapper: wrapper(),
    })

    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(result.current.items).toBeUndefined()
  })

  it("does not fetch at all while disabled", async () => {
    const { fetch } = pager([page(["a"], null)])
    renderHook(() => useCursorList(["rows"], fetch, { enabled: false }), {
      wrapper: wrapper(),
    })

    await new Promise((r) => setTimeout(r, 20))
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe("useCursorList polling", () => {
  it("stops polling once the rows say there is nothing in flight", async () => {
    // A fixed interval keeps asking a question whose answer has stopped changing.
    const rows = [{ id: "a" }]
    const fetch = vi.fn(async () => ({
      items: rows,
      count: 1,
      next_cursor: null,
    }))
    const { result } = renderHook(
      () =>
        useCursorList(["rows"], fetch, {
          pollWhile: () => false,
          pollMs: 10,
        }),
      { wrapper: wrapper() }
    )

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    const after = fetch.mock.calls.length
    await new Promise((r) => setTimeout(r, 60))
    expect(fetch.mock.calls.length).toBe(after)
  })

  it("keeps polling while something is still in flight", async () => {
    const fetch = vi.fn(async () => ({
      items: [{ id: "a" }],
      count: 1,
      next_cursor: null,
    }))
    const { result } = renderHook(
      () =>
        useCursorList(["rows"], fetch, {
          pollWhile: (items) => items.length > 0,
          pollMs: 10,
        }),
      { wrapper: wrapper() }
    )

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    const after = fetch.mock.calls.length
    await waitFor(
      () => expect(fetch.mock.calls.length).toBeGreaterThan(after),
      {
        timeout: 1000,
      }
    )
  })
})

describe("useAllPages", () => {
  it("drains every page into one array", async () => {
    const { fetch } = pager([page(["a"], "1"), page(["b"], null)])
    const { result } = renderHook(() => useAllPages(["rows"], fetch), {
      wrapper: wrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.map((r) => r.id)).toEqual(["a", "b"])
  })

  it("does not collide with the cursor list's cache under the same key", async () => {
    // Both hooks are called with the same base key on the same page; a shared entry
    // would hand a paged list to a control expecting a drained one.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    const w = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)

    const { fetch } = pager([page(["a"], "1"), page(["b"], null)])
    const { result } = renderHook(
      () => ({
        paged: useCursorList(["clusters"], fetch),
        all: useAllPages(["clusters"], fetch),
      }),
      { wrapper: w }
    )

    await waitFor(() => expect(result.current.all.isSuccess).toBe(true))
    await waitFor(() => expect(result.current.paged.items).toBeDefined())

    expect(result.current.all.data?.map((r) => r.id)).toEqual(["a", "b"])
    expect(result.current.paged.items?.map((r) => r.id)).toEqual(["a"])
  })

  it("does not fetch while disabled", async () => {
    const { fetch } = pager([page(["a"], null)])
    renderHook(() => useAllPages(["rows"], fetch, { enabled: false }), {
      wrapper: wrapper(),
    })

    await new Promise((r) => setTimeout(r, 20))
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe("anyTruncated", () => {
  it("is true only when a loaded drain hit the cap", () => {
    const full = Object.assign([1], { truncated: false })
    const capped = Object.assign([1], { truncated: true })
    expect(anyTruncated({ data: full }, { data: undefined })).toBe(false)
    expect(anyTruncated({ data: full }, { data: capped })).toBe(true)
  })
})
