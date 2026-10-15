import { IconRefreshDot } from "@tabler/icons-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { artifactSource, refreshSummary } from "@/modules/demand/display"
import { useRefreshArtifact } from "@/modules/demand/queries"
import type { Artifact } from "@/modules/demand/types"

/**
 * Re-reads the artifact's source and records what the plane can now derive from it.
 *
 * Not `layout/refresh-button`, and deliberately not called Refresh. That one is a read
 * — it drops cache entries and re-runs the queries a page is already showing, and it
 * belongs in `readActions` because it writes nothing. This one is an operation: it
 * reaches HuggingFace and writes the artifact's derived half, so it lives with Edit and
 * Delete and disappears with them in read-only mode. Both on one header with the same
 * word on them would be a trap.
 *
 * Disabled when the artifact records no source, because the plane refuses that with a
 * `422` and there is nothing the person could do differently. The reason is on the
 * button rather than in the failure.
 */
export function RefreshSourceButton({
  artifact,
  label = "Re-read source",
  variant = "outline",
  size,
  className,
}: {
  artifact: Artifact
  label?: string
  variant?: "outline" | "ghost" | "secondary"
  size?: "sm"
  className?: string
}) {
  const refresh = useRefreshArtifact()
  const source = artifactSource(artifact)

  return (
    <Button
      variant={variant}
      size={size}
      className={className}
      disabled={!source || refresh.isPending}
      title={
        source
          ? `Re-read ${source} and record what it says now`
          : "This artifact records no source to re-read"
      }
      onClick={() =>
        refresh.mutate(artifact.id, {
          onSuccess: (result) => {
            /**
             * A refresh that changed nothing is a success and says so, because the
             * alternative is a person pressing it three more times. The warnings are
             * about the source and not about the diff, so they surface either way —
             * a repository that has since become gated is worth hearing on the
             * re-read that found nothing.
             */
            if (result.changed.length === 0)
              toast.info("Nothing new at the source", {
                description: refreshSummary(result.changed),
              })
            else
              toast.success("Artifact re-read", {
                description: refreshSummary(result.changed),
              })
            for (const note of result.notes)
              if (note.level === "warn") toast.warning(note.message)
          },
          onError: (error) =>
            toast.error(formatError(error, "Could not re-read the source")),
        })
      }
    >
      <IconRefreshDot
        className={cn("size-4", refresh.isPending && "animate-spin")}
      />
      {label}
    </Button>
  )
}
