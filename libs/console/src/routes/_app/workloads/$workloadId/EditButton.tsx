import { IconPencil } from "@tabler/icons-react"
import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"

/**
 * Which part of the workload is open for editing.
 *
 * Not one dialog for the whole declared object: it is six questions, each
 * answered by the panel that displays it, and only four of them roll the
 * replicas, which is a distinction a single form cannot make.
 */
export type EditSection =
  | null
  | "details"
  | "placement"
  | "engine"
  | "args"
  | "serving"
  | "capabilities"

/** A panel's own way into the dialog that edits what it shows. */
export function EditButton({
  label,
  onClick,
}: {
  label: string
  onClick: () => void
}) {
  return (
    <WriteAction>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        title={label}
        onClick={onClick}
      >
        <IconPencil />
      </Button>
    </WriteAction>
  )
}
