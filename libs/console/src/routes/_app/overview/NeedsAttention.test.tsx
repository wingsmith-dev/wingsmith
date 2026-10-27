import type { AttentionItem } from "@/modules/overview/attention"
import { fireEvent, render, screen } from "@/test/render"
import type { ReactNode } from "react"
import { describe, expect, it, vi } from "vitest"
import { NeedsAttention } from "./NeedsAttention"

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="#">{children}</a>,
}))

const ALL_CLEAR = /Every cluster reports in/

const unreachable: AttentionItem = {
  id: "cluster:c1",
  group: "health",
  tone: "danger",
  kind: "cluster",
  title: "c1 unreachable",
  detail: "last seen 20m ago",
  action: { label: "Open", link: { to: "/clusters" } },
}

function renderPanel(
  props: Partial<React.ComponentProps<typeof NeedsAttention>> = {}
) {
  return render(
    <NeedsAttention
      items={[]}
      isLoading={false}
      error={null}
      clusterCount={2}
      incomplete={false}
      {...props}
    />
  )
}

describe("NeedsAttention", () => {
  it("claims an all-clear when every source loaded and clusters exist", () => {
    renderPanel()
    expect(screen.getByText(ALL_CLEAR)).toBeInTheDocument()
  })

  it("does not claim it on the discovered tab while health items exist", () => {
    renderPanel({ items: [unreachable] })
    fireEvent.click(screen.getByRole("button", { name: /Discovered/ }))
    expect(screen.queryByText(ALL_CLEAR)).toBeNull()
    expect(screen.getByText("No discovered workloads.")).toBeInTheDocument()
  })

  it("does not claim it with no clusters registered", () => {
    renderPanel({ clusterCount: 0 })
    expect(screen.queryByText(ALL_CLEAR)).toBeNull()
    expect(screen.getByText("No clusters registered.")).toBeInTheDocument()
  })

  it("does not claim it when a source failed, and says the list may be short", () => {
    renderPanel({ incomplete: true })
    expect(screen.queryByText(ALL_CLEAR)).toBeNull()
    expect(screen.getByRole("status")).toHaveTextContent(/may be incomplete/)
  })
})
