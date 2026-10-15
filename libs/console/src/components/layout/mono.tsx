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
    <span className={cn("font-mono text-2xs text-muted-foreground", className)}>
      {value}
    </span>
  )
}

export function Labels({ labels }: { labels: Record<string, string> }) {
  const entries = Object.entries(labels)
  if (entries.length === 0) {
    return (
      <span className="font-mono text-2xs text-muted-foreground">none</span>
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
    return <span className="text-muted-foreground">—</span>
  }
  return <>{children}</>
}

/** A monospace value held to one line; the whole string is on hover. */
export function Truncated({
  value,
  className,
}: {
  value: string
  className?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("block max-w-full truncate", className)}>
          {value}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm font-mono text-2xs break-all">
        {value}
      </TooltipContent>
    </Tooltip>
  )
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
      <span className={behind ? "text-warning" : ""}>{observed ?? "—"}</span>
      <span className="text-muted-foreground"> / {desired}</span>
    </span>
  )
}

type BlockLanguage = "yaml" | "shell"

/**
 * A tiny, dependency-free highlighter for the two languages the console shows
 * in blocks: `values.yaml` and `helm`/`kubectl` commands. Deliberately not a
 * general parser — it colours the few token classes that make these snippets
 * readable (comments, keys, values; commands, flags) and leaves the rest plain.
 */
function highlightYaml(line: string, key: number): React.ReactNode {
  const trimmed = line.trimStart()
  if (trimmed === "") return line
  if (trimmed.startsWith("#")) {
    return (
      <span key={key} className="text-muted-foreground italic">
        {line}
      </span>
    )
  }
  const match = line.match(/^(\s*-?\s*)([\w.\-/]+)(:)(.*)$/)
  if (!match) return line
  const [, indent, name, colon, rest] = match
  // Split a trailing ` # comment` off the value so it colours separately.
  const commentAt = rest.indexOf(" #")
  const val = commentAt >= 0 ? rest.slice(0, commentAt) : rest
  const comment = commentAt >= 0 ? rest.slice(commentAt) : ""
  return (
    <span key={key}>
      {indent}
      <span className="text-sky-600 dark:text-sky-400">{name}</span>
      {colon}
      {val && <span className="text-success">{val}</span>}
      {comment && (
        <span className="text-muted-foreground italic">{comment}</span>
      )}
    </span>
  )
}

function highlightShell(line: string, key: number): React.ReactNode {
  const parts = line.split(/(\s+)/)
  let sawCommand = false
  return (
    <span key={key}>
      {parts.map((part, i) => {
        if (/^\s+$/.test(part) || part === "") return part
        if (part.startsWith("-")) {
          return (
            <span key={i} className="text-warning">
              {part}
            </span>
          )
        }
        if (!sawCommand) {
          sawCommand = true
          return (
            <span
              key={i}
              className="font-medium text-violet-600 dark:text-violet-400"
            >
              {part}
            </span>
          )
        }
        return part
      })}
    </span>
  )
}

function highlight(value: string, language: BlockLanguage): React.ReactNode {
  const fn = language === "yaml" ? highlightYaml : highlightShell
  const lines = value.split("\n")
  return lines.map((line, i) => (
    <React.Fragment key={i}>
      {fn(line, i)}
      {i < lines.length - 1 ? "\n" : null}
    </React.Fragment>
  ))
}

/**
 * A multi-line command block with a copy button. Unlike {@link CopyableId} the
 * whole snippet is always shown; the button copies it verbatim so an operator
 * can paste a `kubectl` or `just` line without hand-retyping a cluster name.
 * Pass `language` to colour a `values.yaml` or a shell command; omit it for
 * plain text (a token, an id).
 */
export function CopyableBlock({
  value,
  language,
  className,
}: {
  value: string
  language?: BlockLanguage
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
        {language ? highlight(value, language) : value}
      </pre>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy"}
        className="absolute top-2 right-2 text-muted-foreground opacity-0 transition-opacity group-hover/block:opacity-100 hover:text-foreground focus-visible:opacity-100"
      >
        {copied ? (
          <IconCheck className="size-3.5 text-success" />
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
        className="shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover/id:opacity-100 hover:text-foreground focus-visible:opacity-100"
      >
        {copied ? (
          <IconCheck className="size-3 text-success" />
        ) : (
          <IconCopy className="size-3" />
        )}
      </button>
    </span>
  )
}
