/** The Hangar wordmark glyph: three slabs, drawn in the current text color. */
export function HangarMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 195 185"
      fill="currentColor"
      aria-hidden="true"
      className={className}
    >
      <path d="M58 25 58 138 20 162 20 50Z" />
      <path d="M114 68 114 142 74 122 74 88Z" />
      <path d="M130 10 170 30 170 172 130 152Z" />
    </svg>
  )
}
