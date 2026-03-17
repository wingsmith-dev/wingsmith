import { SidebarTrigger } from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"

interface PageHeaderProps {
  title: string
  description?: string
  actions?: React.ReactNode
  className?: string
}

export function PageHeader({
  title,
  description,
  actions,
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
          <h1 className="text-md font-semibold leading-none tracking-tight">
            {title}
          </h1>
          {description && (
            <p className="mt-0.5 font-mono text-xs text-muted-foreground/60">
              {description}
            </p>
          )}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
    </header>
  )
}
