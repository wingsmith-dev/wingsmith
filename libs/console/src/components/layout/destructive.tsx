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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"
import {
  ResourceIcon,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import { formatError } from "@/lib/format"
import { useReadOnly } from "@/modules/settings/store"
import { IconDotsVertical } from "@tabler/icons-react"

/**
 * Deleting a declared object is how supply and demand get withdrawn, so the
 * name is spelled back to the operator before they confirm it.
 */
export function DeleteDialog({
  open,
  onOpenChange,
  name,
  kind,
  icon,
  note,
  error,
  pending,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  name: string
  kind: string
  icon?: ResourceKind
  note?: string
  error?: unknown
  pending?: boolean
  onConfirm: () => void
}) {
  const readOnly = useReadOnly()
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            {icon && (
              <ResourceIcon kind={icon} className="text-muted-foreground" />
            )}
            Delete {kind}?
          </AlertDialogTitle>
          <AlertDialogDescription>
            <span className="font-mono text-foreground">{name}</span> will be
            removed from the plane. {note}
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
            onClick={(e) => {
              e.preventDefault()
              onConfirm()
            }}
            disabled={pending || readOnly}
            className="bg-destructive text-white hover:bg-destructive/90"
          >
            {pending ? "Deleting…" : "Delete"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export interface RowAction {
  label: string
  onSelect: () => void
  destructive?: boolean
}

/**
 * The trailing `⋮` on a row or card. Stops the click reaching the row link, and
 * disappears entirely in read-only mode — every action it carries is a write.
 */
export function RowActions({ actions }: { actions: RowAction[] }) {
  const readOnly = useReadOnly()
  if (readOnly || actions.length === 0) return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 text-muted-foreground hover:text-foreground"
          aria-label="Actions"
        >
          <IconDotsVertical className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        {actions.map((action, index) => (
          <div key={action.label}>
            {action.destructive && index > 0 && <DropdownMenuSeparator />}
            <DropdownMenuItem
              variant={action.destructive ? "destructive" : undefined}
              onSelect={action.onSelect}
            >
              {action.label}
            </DropdownMenuItem>
          </div>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
