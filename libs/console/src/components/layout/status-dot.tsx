import { cn } from "@/lib/utils"

export type StatusTone = "ok" | "warn" | "danger" | "neutral" | "info"

const TONE_CLASS: Record<StatusTone, string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  danger: "bg-destructive",
  info: "bg-primary",
  neutral: "bg-muted-foreground/40",
}

export function StatusDot({
  tone,
  pulse = false,
  className,
}: {
  tone: StatusTone
  pulse?: boolean
  className?: string
}) {
  return (
    <span className={cn("relative flex size-2 shrink-0", className)}>
      {pulse && (
        <span
          className={cn(
            "absolute inline-flex size-full animate-ping rounded-full opacity-60 motion-reduce:animate-none",
            TONE_CLASS[tone]
          )}
        />
      )}
      <span
        className={cn(
          "relative inline-flex size-2 rounded-full",
          TONE_CLASS[tone]
        )}
      />
    </span>
  )
}

export function StatusLabel({
  tone,
  children,
  pulse,
}: React.PropsWithChildren<{ tone: StatusTone; pulse?: boolean }>) {
  return (
    <span className="inline-flex items-center gap-2 font-mono text-xs">
      <StatusDot tone={tone} pulse={pulse} />
      {children}
    </span>
  )
}
