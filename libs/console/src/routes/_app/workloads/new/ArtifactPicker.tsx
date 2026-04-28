import { Id } from "@/components/layout/mono"
import { ResourceIcon } from "@/components/layout/resource-icon"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { shortenId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { formatBytes } from "@/modules/demand/display"
import { useArtifacts } from "@/modules/demand/queries"
import type { Artifact } from "@/modules/demand/types"
import { useState } from "react"

/**
 * Which weights to run — the first question, and the only one with no default.
 *
 * A list rather than a select. A select is the right control for a closed set of a
 * dozen; an artifact catalogue is neither closed nor small, and the things that tell
 * two artifacts apart — parameter count, format, whether the digest was verified — do
 * not fit on one line of a dropdown. The search box filters server-side, because the
 * catalogue outgrows one page long before it outgrows one screen.
 */
export function ArtifactPicker({
  onPick,
}: {
  onPick: (artifact: Artifact) => void
}) {
  const [search, setSearch] = useState("")
  const list = useArtifacts(search ? { search } : {})
  const artifacts = list.items ?? []

  return (
    <div className="space-y-2">
      <Input
        autoFocus
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search artifacts by name or digest"
        className="h-8 font-mono text-xs"
      />

      {list.isLoading && <Skeleton className="h-32 w-full" />}

      {!list.isLoading && artifacts.length === 0 && (
        <p className="py-6 text-center font-mono text-2xs text-muted-foreground/60">
          {search
            ? "Nothing matches."
            : "No artifacts registered. Register one first — there is nothing to run yet."}
        </p>
      )}

      {artifacts.length > 0 && (
        <div className="max-h-72 divide-y overflow-y-auto rounded-lg border">
          {artifacts.map((artifact) => (
            <ArtifactRow
              key={artifact.id}
              artifact={artifact}
              onPick={onPick}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ArtifactRow({
  artifact,
  onPick,
}: {
  artifact: Artifact
  onPick: (artifact: Artifact) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(artifact)}
      className={cn(
        "flex w-full items-center gap-3 px-3 py-2 text-left transition-colors",
        "hover:bg-primary/5 focus-visible:bg-primary/5 focus-visible:outline-none"
      )}
    >
      <ResourceIcon
        kind="artifact"
        className="size-4 shrink-0 text-muted-foreground/60"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate font-mono text-xs">{artifact.name}</span>
          {artifact.verified && (
            <Badge variant="outline" className="font-mono text-2xs">
              verified
            </Badge>
          )}
        </div>
        <Id value={shortenId(artifact.digest)} />
      </div>
      <ArtifactFacts artifact={artifact} />
    </button>
  )
}

/** The three things that tell two artifacts apart at a glance. */
function ArtifactFacts({ artifact }: { artifact: Artifact }) {
  const params = artifact.parameter_count
  return (
    <div className="flex shrink-0 flex-col items-end font-mono text-2xs text-muted-foreground/60 tabular-nums">
      <span>
        {params ? `${(params / 1e9).toFixed(params < 1e10 ? 1 : 0)}B` : "—"}
      </span>
      <span>
        {artifact.size_bytes ? formatBytes(artifact.size_bytes) : artifact.kind}
      </span>
    </div>
  )
}
