import { cn } from "@/lib/utils"
import { TIME_WINDOWS, type TimeWindow } from "@/modules/metrics/windows"

/**
 * Which span the charts beside it are drawn over.
 *
 * It changes the question, not the answer: every window is a fresh query to the
 * store instead of a slice of one already held, because a rate over an hour and
 * a rate over a week are two different measurements and neither is the other
 * resampled.
 */
export function WindowToggle({
  value,
  onChange,
  className,
}: {
  value: TimeWindow
  onChange: (window: TimeWindow) => void
  className?: string
}) {
  return (
    <div
      role="group"
      aria-label="Chart window"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-md border bg-muted/40 p-0.5",
        className
      )}
    >
      {TIME_WINDOWS.map((window) => (
        <button
          key={window.key}
          type="button"
          aria-pressed={window.key === value.key}
          onClick={() => onChange(window)}
          className={cn(
            "rounded-[3px] px-2 py-0.5 font-mono text-2xs text-muted-foreground transition-colors",
            "hover:text-foreground",
            window.key === value.key &&
              "bg-background text-foreground shadow-xs"
          )}
        >
          {window.label}
        </button>
      ))}
    </div>
  )
}
