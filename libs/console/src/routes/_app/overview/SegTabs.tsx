import { cn } from "@/lib/utils"

export interface SegOption<T extends string> {
  value: T
  label: string
  /** Shown dimmed after the label, for tabs that filter a counted list. */
  count?: number
}

interface SegTabsProps<T extends string> {
  options: SegOption<T>[]
  value: T
  onChange: (value: T) => void
  /** `mono` is the tighter, monospaced row the chart headers use. */
  variant?: "default" | "mono"
  className?: string
  "aria-label"?: string
}

/**
 * A segmented control, local to the overview.
 *
 * Deliberately not `components/ui/tabs`: nothing here switches a panel of
 * content, these are filters over one list and a time window over one chart, and
 * a tablist with no tabpanel is the wrong thing to hand a screen reader.
 */
export function SegTabs<T extends string>({
  options,
  value,
  onChange,
  variant = "default",
  className,
  "aria-label": ariaLabel,
}: SegTabsProps<T>) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex shrink-0 flex-wrap gap-0.5 rounded-md bg-secondary p-0.5",
        className
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-sm text-muted-foreground transition-colors hover:text-foreground aria-pressed:bg-card aria-pressed:text-foreground",
            variant === "mono"
              ? "px-2 py-1 font-mono text-2xs"
              : "px-2.5 py-1 text-xs"
          )}
        >
          {option.label}
          {option.count !== undefined && (
            <span className="font-mono text-muted-foreground">
              {option.count}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
