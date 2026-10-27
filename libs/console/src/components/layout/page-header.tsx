import { SidebarTrigger } from "@/components/ui/sidebar"
import { WriteAction } from "@/components/layout/write-action"
import { cn } from "@/lib/utils"

interface PageHeaderProps {
  title: React.ReactNode
  description?: string
  /**
   * A way back out, drawn to the left of the title *and* its description.
   *
   * Inside `title` it would sit on the title's own line, leaving the id
   * underneath it, not beside it: the arrow leaves the whole heading,
   * so it belongs next to the whole heading.
   */
  back?: React.ReactNode
  /** Suppressed in read-only mode: every header action starts a write. */
  actions?: React.ReactNode
  /** Actions that read, not write, and so survive read-only mode. */
  readActions?: React.ReactNode
  className?: string
}

export function PageHeader({
  title,
  description,
  back,
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
      {/* Only where the sidebar is a sheet. Wider than that the sidebar carries
          its own collapse control, and a second one here would put a chrome
          button in front of the page's name on every route. */}
      <SidebarTrigger className="-ml-1 size-7 text-muted-foreground hover:text-foreground md:hidden" />
      <div className="h-4 w-px bg-border md:hidden" />
      <div className="flex flex-1 items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          {back}
          <div className="min-w-0">
            <h1 className="text-md leading-none font-semibold tracking-tight">
              {title}
            </h1>
            {description && (
              <p className="mt-1 font-mono text-2xs text-muted-foreground">
                {description}
              </p>
            )}
          </div>
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
