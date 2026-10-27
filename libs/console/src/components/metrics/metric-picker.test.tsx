import { render, screen } from "@/test/render"
import { metricGroups } from "@/modules/metrics/catalog"
import userEvent from "@testing-library/user-event"
import { beforeAll, describe, expect, it, vi } from "vitest"

import { MetricPicker } from "./metric-picker"

beforeAll(() => {
  // Radix drives its menus with pointer capture, which jsdom does not implement.
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
  Element.prototype.scrollIntoView = () => {}
})

const INFERENCE = metricGroups("inference").flatMap((group) => group.metrics)
const ALL = INFERENCE.map((metric) => metric.name)

async function open(
  props: Partial<Parameters<typeof MetricPicker>[0]> = {}
): Promise<() => string[][]> {
  const onChange = vi.fn()
  render(
    <MetricPicker
      family="inference"
      selected={["inference.ttft"]}
      available={ALL}
      onChange={onChange}
      {...props}
    />
  )
  await userEvent.click(screen.getByRole("button", { name: /metrics/i }))
  return () => onChange.mock.calls.map((call) => call[0] as string[])
}

describe("MetricPicker", () => {
  it("offers every registered metric in the family, grouped", async () => {
    await open()
    for (const metric of INFERENCE) {
      expect(
        screen.getByRole("menuitemcheckbox", {
          name: new RegExp(`${metric.name.replace(/\./g, "\\.")} · `),
        }),
        metric.name
      ).toBeInTheDocument()
    }
    expect(screen.getByText("Latency")).toBeInTheDocument()
    expect(screen.getByText("Request shape")).toBeInTheDocument()
  })

  it("shows each metric's unit beside its name", async () => {
    await open()
    expect(
      screen.getByRole("menuitemcheckbox", {
        name: /inference\.queue_time · seconds/,
      })
    ).toBeInTheDocument()
    expect(
      screen.getByRole("menuitemcheckbox", {
        name: /inference\.output_tokens_per_stream · per second/,
      })
    ).toBeInTheDocument()
  })

  it("keeps the two throughput measurements as two separate choices", async () => {
    const calls = await open({ selected: [] })
    const aggregate = screen.getByRole("menuitemcheckbox", {
      name: /Output tokens\/sec, all replicas/,
    })
    const perStream = screen.getByRole("menuitemcheckbox", {
      name: /Output tokens\/sec, one stream/,
    })
    expect(aggregate).not.toBe(perStream)

    await userEvent.click(perStream)
    expect(calls()).toEqual([["inference.output_tokens_per_stream"]])
    expect(aggregate).toHaveAttribute("data-state", "unchecked")
  })

  it("adds a metric in catalog order, not at the end", async () => {
    const calls = await open({ selected: ["inference.requests_running"] })
    await userEvent.click(
      screen.getByRole("menuitemcheckbox", {
        name: /Time to first token \(p50\)/,
      })
    )
    expect(calls()).toEqual([
      ["inference.ttft_p50", "inference.requests_running"],
    ])
  })

  it("removes a metric that was chosen", async () => {
    const calls = await open({ selected: ["inference.ttft", "inference.tpot"] })
    await userEvent.click(
      screen.getByRole("menuitemcheckbox", {
        name: /Time to first token \(p95\)/,
      })
    )
    expect(calls()).toEqual([["inference.tpot"]])
  })

  it("offers a metric the store answered nothing for, disabled and said so", async () => {
    await open({
      available: ALL.filter((name) => name !== "inference.preemptions"),
    })
    const item = screen.getByRole("menuitemcheckbox", {
      name: /inference\.preemptions/,
    })
    expect(item).toHaveAttribute("data-disabled")
    expect(item.textContent).toMatch(/no series/)
  })
})
