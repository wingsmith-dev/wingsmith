import { SidebarTrigger } from "@/components/ui/sidebar"
import { WriteAction } from "@/components/layout/write-action"
import { cn } from "@/lib/utils"

interface PageHeaderProps {
  title: React.ReactNode
  description?: string
  /** Suppressed in read-only mode: every header action starts a write. */
  actions?: React.ReactNode
  /** Actions that read rather than write, and so survive read-only mode. */
  readActions?: React.ReactNode
  className?: string
}

export function PageHeader({
  title,
  description,
  actions,
  readActions,
  className,
}: PageHeaderProps) {
  return (
    <header
      className={cn(
        "flex h-16 shrink-0 items-center gap-3 border-b bg-card px-4",
        className
      )}
    >
      <SidebarTrigger className="-ml-1 size-7 text-muted-foreground hover:text-foreground" />
      <div className="h-4 w-px bg-border" />
      <div className="flex flex-1 items-center justify-between gap-4">
        <div>
          <h1 className="text-md leading-none font-semibold tracking-tight">
            {title}
          </h1>
          {description && (
            <p className="mt-1 font-mono text-2xs text-muted-foreground/60">
              {description}
            </p>
          )}
        </div>
        {(actions || readActions) && (
          <div className="flex items-center gap-2">
            {readActions}
            {actions && <WriteAction>{actions}</WriteAction>}
          </div>
        )}
      </div>
    </header>
  )
}
