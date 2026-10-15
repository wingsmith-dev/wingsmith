import { act, render, screen, waitFor } from "@/test/render"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { CopyableId, Dash, Id, Labels, VersionPair } from "./mono"

const FULL = "dev_01j9x2aaaaaaaaaaaaaaaaaaaa"

describe("Id", () => {
  it("renders the whole opaque value", () => {
    render(<Id value={FULL} />)
    expect(screen.getByText(FULL)).toBeInTheDocument()
  })
})

describe("Labels", () => {
  it("draws one badge per label", () => {
    render(<Labels labels={{ zone: "a", pool: "gpu" }} />)
    expect(screen.getByText("zone=a")).toBeInTheDocument()
    expect(screen.getByText("pool=gpu")).toBeInTheDocument()
  })

  it("says none rather than drawing an empty row", () => {
    render(<Labels labels={{}} />)
    expect(screen.getByText("none")).toBeInTheDocument()
  })
})

describe("Dash", () => {
  it("draws an em dash for nothing", () => {
    // A blank cell reads as a rendering bug; an em dash reads as "no value".
    for (const empty of [null, undefined, ""]) {
      const { unmount } = render(<Dash>{empty}</Dash>)
      expect(screen.getByText("—")).toBeInTheDocument()
      unmount()
    }
  })

  it("passes a real value straight through", () => {
    render(<Dash>iad-1</Dash>)
    expect(screen.getByText("iad-1")).toBeInTheDocument()
    expect(screen.queryByText("—")).not.toBeInTheDocument()
  })

  it("does not swallow a legitimate zero", () => {
    render(<Dash>{0}</Dash>)
    expect(screen.getByText("0")).toBeInTheDocument()
  })
})

describe("VersionPair", () => {
  it("shows both halves, because one column cannot say what we asked for and what came back", () => {
    render(<VersionPair desired="1.4.0" observed="1.3.1" behind />)
    expect(screen.getByText("1.3.1")).toBeInTheDocument()
    expect(screen.getByText(/1\.4\.0/)).toBeInTheDocument()
  })

  it("draws an em dash when nothing has been observed yet", () => {
    render(<VersionPair desired="1.4.0" observed={null} behind={false} />)
    expect(screen.getByText("—")).toBeInTheDocument()
  })

  it("marks a version that is behind and leaves a matching one plain", () => {
    const { container: behind } = render(
      <VersionPair desired="1.4.0" observed="1.3.1" behind />
    )
    const { container: level } = render(
      <VersionPair desired="1.4.0" observed="1.4.0" behind={false} />
    )
    expect(behind.innerHTML).toContain("text-warning")
    expect(level.innerHTML).not.toContain("text-warning")
  })
})

describe("CopyableId", () => {
  // jsdom defines navigator.clipboard as a getter-only property, so it has to be
  // redefined rather than assigned.
  function withClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    })
    return writeText
  }

  it("shows the short form and offers the whole value to copy", () => {
    withClipboard()
    render(<CopyableId value={FULL} />)

    expect(screen.getByText("dev_01j9x2")).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: `Copy ${FULL}` })
    ).toBeInTheDocument()
  })

  it("copies the full identifier, not the shortened one", () => {
    // Copying the truncated string would hand over something that resolves to nothing.
    const writeText = withClipboard()
    render(<CopyableId value={FULL} />)

    return userEvent
      .click(screen.getByRole("button", { name: `Copy ${FULL}` }))
      .then(() => {
        expect(writeText).toHaveBeenCalledWith(FULL)
      })
  })

  it("confirms the copy and then goes back to offering it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // setup() installs its own clipboard stub, so the spy goes in after it.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const writeText = withClipboard()
    render(<CopyableId value={FULL} />)

    await user.click(screen.getByRole("button", { name: `Copy ${FULL}` }))
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: `Copied ${FULL}` })
      ).toBeInTheDocument()
    )

    act(() => {
      vi.advanceTimersByTime(1500)
    })
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: `Copy ${FULL}` })
      ).toBeInTheDocument()
    )
    expect(writeText).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it("honours a requested width", () => {
    withClipboard()
    render(<CopyableId value={FULL} chars={10} />)
    expect(screen.getByText("dev_01j9x2aaaa")).toBeInTheDocument()
  })

  it("lets children replace the rendered text so an id can navigate", () => {
    withClipboard()
    render(
      <CopyableId value={FULL}>
        <a href="/devices/1">iad-1-n01 · gpu 0</a>
      </CopyableId>
    )
    expect(
      screen.getByRole("link", { name: "iad-1-n01 · gpu 0" })
    ).toBeInTheDocument()
    expect(screen.queryByText("dev_01j9x2")).not.toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: `Copy ${FULL}` })
    ).toBeInTheDocument()
  })

  it("does not let the copy button trigger the row it sits in", async () => {
    // The button lives inside clickable table rows; a bubbled click would navigate away
    // from the page the operator was copying an id on.
    withClipboard()
    const rowClicked = vi.fn()
    render(
      <div onClick={rowClicked}>
        <CopyableId value={FULL} />
      </div>
    )

    await userEvent.click(screen.getByRole("button", { name: `Copy ${FULL}` }))
    expect(rowClicked).not.toHaveBeenCalled()
  })
})
