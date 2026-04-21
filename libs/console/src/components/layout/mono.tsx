import * as React from "react"

import { Badge } from "@/components/ui/badge"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { shortenId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { IconCheck, IconCopy } from "@tabler/icons-react"

/** A prefixed id, rendered as the opaque string it is. */
export function Id({
  value,
  className,
}: {
  value: string
  className?: string
}) {
  return (
    <span
      className={cn("font-mono text-2xs text-muted-foreground/60", className)}
    >
      {value}
    </span>
  )
}

export function Labels({ labels }: { labels: Record<string, string> }) {
  const entries = Object.entries(labels)
  if (entries.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground/40">none</span>
    )
  }
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([key, value]) => (
        <Badge key={key} variant="outline" className="font-mono text-2xs">
          {key}={value}
        </Badge>
      ))}
    </div>
  )
}

export function Dash({ children }: { children: React.ReactNode }) {
  if (children === null || children === undefined || children === "") {
    return <span className="text-muted-foreground/40">—</span>
  }
  return <>{children}</>
}

/**
 * A declared value and its observed counterpart, kept side by side. They are
 * never merged: `desired` is what we asked for and `observed` is what came
 * back, and one nullable column cannot say both.
 */
export function VersionPair({
  desired,
  observed,
  behind,
}: {
  desired: string
  observed: string | null
  behind: boolean
}) {
  return (
    <span className="font-mono text-2xs">
      <span className={behind ? "text-amber-600 dark:text-amber-400" : ""}>
        {observed ?? "—"}
      </span>
      <span className="text-muted-foreground/40"> / {desired}</span>
    </span>
  )
}

/**
 * A multi-line command block with a copy button. Unlike {@link CopyableId} the
 * whole snippet is always shown; the button copies it verbatim so an operator
 * can paste a `kubectl` or `just` line without hand-retyping a cluster name.
 */
export function CopyableBlock({
  value,
  className,
}: {
  value: string
  className?: string
}) {
  const [copied, setCopied] = React.useState(false)
  const timer = React.useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  )

  React.useEffect(() => () => clearTimeout(timer.current), [])

  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <div className={cn("group/block relative", className)}>
      <pre className="overflow-x-auto rounded-md bg-muted/50 p-3 pr-9 font-mono text-2xs leading-relaxed ring-1 ring-foreground/10">
        {value}
      </pre>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy"}
        className="absolute top-2 right-2 text-muted-foreground/50 opacity-0 transition-opacity group-hover/block:opacity-100 hover:text-foreground focus-visible:opacity-100"
      >
        {copied ? (
          <IconCheck className="size-3.5 text-emerald-500" />
        ) : (
          <IconCopy className="size-3.5" />
        )}
      </button>
    </div>
  )
}

/**
 * A shortened identifier with the whole thing on hover and a copy button that
 * appears with it. `children` replaces the rendered text — pass a `Link` when
 * the id should navigate, and the copy button stays outside it so clicking one
 * never triggers the other.
 */
export function CopyableId({
  value,
  chars,
  className,
  children,
}: {
  value: string
  chars?: number
  className?: string
  children?: React.ReactNode
}) {
  const [copied, setCopied] = React.useState(false)
  const timer = React.useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  )

  React.useEffect(() => () => clearTimeout(timer.current), [])

  const copy = (event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <span className="group/id inline-flex items-center gap-1">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn("font-mono", className)}>
            {children ?? shortenId(value, chars)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="font-mono text-2xs">{value}</TooltipContent>
      </Tooltip>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? `Copied ${value}` : `Copy ${value}`}
        className="shrink-0 text-muted-foreground/50 opacity-0 transition-opacity group-hover/id:opacity-100 hover:text-foreground focus-visible:opacity-100"
      >
        {copied ? (
          <IconCheck className="size-3 text-emerald-500" />
        ) : (
          <IconCopy className="size-3" />
        )}
      </button>
    </span>
  )
}
