import { cn } from "@/lib/utils"

export function Field({
  label,
  children,
  className,
}: React.PropsWithChildren<{ label: string; className?: string }>) {
  return (
    <div className={cn("space-y-0.5", className)}>
      <div className="font-mono text-2xs uppercase tracking-wider text-muted-foreground/60">
        {label}
      </div>
      <div className="font-mono text-xs">{children}</div>
    </div>
  )
}

export function FieldGrid({ children }: React.PropsWithChildren) {
  return <div className="grid gap-3 sm:grid-cols-2">{children}</div>
}
