import type { KeyboardEvent, MouseEvent } from "react"

const INTERACTIVE = "a, button, input, select, textarea, [role=button]"

/** Whether the event started on a control nested inside the activatable element. */
function fromInnerControl(event: {
  target: EventTarget
  currentTarget: Element
}) {
  const control =
    event.target instanceof Element ? event.target.closest(INTERACTIVE) : null
  return (
    !!control &&
    control !== event.currentTarget &&
    event.currentTarget.contains(control)
  )
}

/**
 * Makes a clickable element that cannot be a `<button>` (a table row, a board
 * row) reachable and operable from the keyboard: it takes focus, and Enter or
 * Space does what a click does.
 *
 * Clicks and keys on a control inside the element are left to that control, so
 * a button in a row does not also activate the row.
 */
export function activationProps(onActivate: (() => void) | undefined) {
  if (!onActivate) return {}
  return {
    tabIndex: 0,
    onClick: (event: MouseEvent<HTMLElement>) => {
      if (fromInnerControl(event)) return
      onActivate()
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (event.target !== event.currentTarget) return
      if (event.key !== "Enter" && event.key !== " ") return
      event.preventDefault()
      onActivate()
    },
  }
}

/** The focus ring for an element made focusable by {@link activationProps}. */
export const ACTIVATION_FOCUS =
  "outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
