import { useState } from "react"
import { toast } from "sonner"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { WriteAction } from "@/components/layout/write-action"
import { formatError } from "@/lib/format"

/**
 * The manager that applied this object (ADR 0033), and the way to take it
 * over. Taking over sends `force`, which clears the manager and the
 * last-applied record, so the next apply from that manager is refused until it
 * forces back.
 */
export function ManagedBy({
  kind,
  name,
  manager,
  principal,
  pending,
  error,
  onTakeOver,
}: {
  kind: string
  name: string
  manager: string | null | undefined
  principal?: string | null
  pending?: boolean
  error?: unknown
  onTakeOver: (done: () => void) => void
}) {
  const [open, setOpen] = useState(false)
  if (!manager) return null
  return (
    <div className="flex items-center gap-2">
      <Badge
        variant="outline"
        title={principal ? `Last applied by ${principal}` : undefined}
      >
        Managed by {manager}
      </Badge>
      <WriteAction>
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          Take over
        </Button>
      </WriteAction>
      <AlertDialog open={open} onOpenChange={setOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Take over this {kind}?</AlertDialogTitle>
            <AlertDialogDescription>
              <span className="font-mono text-foreground">{name}</span> is
              applied by <span className="font-mono">{manager}</span>. Taking it
              over lets the console edit it, and the next apply from {manager}{" "}
              is refused until it forces back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {!!error && (
            <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {formatError(error)}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                onTakeOver(() => {
                  setOpen(false)
                  toast.success(`Took over ${kind}`, { description: name })
                })
              }}
            >
              Take over
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
