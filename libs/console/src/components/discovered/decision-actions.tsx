import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"
import { formatError } from "@/lib/format"
import { workloadLabel } from "@/modules/discovered/display"
import {
  useDismissWorkload,
  useIgnoreWorkload,
  useRestoreWorkload,
  useTrackWorkload,
} from "@/modules/discovered/queries"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import { PromoteDialog } from "./promote-dialog"
import { ServingDraftDialog } from "./serving-draft-dialog"
import { toast } from "sonner"

/**
 * Track, ignore, remove, promote. All but the last record a decision beside the
 * sighting and send nothing to a cluster; only promote produces declared state,
 * which is why it is the one that opens a dialog rather than firing on click.
 *
 * Remove is offered on anything not yet promoted and is undone by Restore, which
 * is the only action a removed sighting shows — it is still running, and the
 * workload underneath was never touched.
 */
export function DecisionActions({
  workload,
}: {
  workload: DiscoveredWorkload
}) {
  const track = useTrackWorkload()
  const ignore = useIgnoreWorkload()
  const dismiss = useDismissWorkload()
  const restore = useRestoreWorkload()
  const label = workloadLabel(workload)
  const busy =
    track.isPending ||
    ignore.isPending ||
    dismiss.isPending ||
    restore.isPending

  const decide =
    (mutation: typeof track, verb: string): (() => void) =>
    () =>
      mutation.mutate(
        { ref: workload.id },
        {
          onSuccess: () => toast.success(`${label} ${verb}`),
          onError: (error) =>
            toast.error(
              formatError(error, `Could not ${verb.slice(0, -1)} it`)
            ),
        }
      )

  return (
    <WriteAction>
      <div className="flex items-center justify-end gap-1.5">
        {workload.status === "dismissed" && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={decide(restore, "restored")}
          >
            Restore
          </Button>
        )}
        {workload.status !== "promoted" && workload.status !== "dismissed" && (
          <>
            {workload.status !== "tracked" && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={decide(track, "tracked")}
              >
                Track
              </Button>
            )}
            {workload.status !== "ignored" && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={decide(ignore, "ignored")}
              >
                Ignore
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={decide(dismiss, "removed")}
            >
              Remove
            </Button>
            <ServingDraftDialog workload={workload} />
            <PromoteDialog workload={workload} />
          </>
        )}
      </div>
    </WriteAction>
  )
}
