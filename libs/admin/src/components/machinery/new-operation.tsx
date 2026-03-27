import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { formatError } from "@/lib/format"
import { useCreateOperation } from "@/modules/machinery/queries"
import {
  OPERATION_TARGET_KINDS,
  OPERATION_TYPES,
  qualifyTarget,
  targetMismatch,
  type OperationType,
} from "@/modules/machinery/types"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

const TYPES = Object.keys(OPERATION_TYPES) as OperationType[]

/**
 * `POST /v1/operations` with a type and a target — never a verb on the target's
 * own path. What comes back is a thing with a lifecycle to watch, so from the
 * global operations page the caller is sent to it. Fired from the resource the
 * operation acts on, `stayOnPage` keeps them where they are and hands them the
 * link instead: one component, two dispositions.
 */
export function NewOperationDialog({
  defaultType,
  defaultTarget,
  trigger,
  stayOnPage,
}: {
  defaultType?: OperationType
  defaultTarget?: string
  trigger?: React.ReactNode
  stayOnPage?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [type, setType] = useState<OperationType>(defaultType ?? "node.drain")
  const [target, setTarget] = useState(defaultTarget ?? "")
  const [reason, setReason] = useState("")
  const create = useCreateOperation()
  const navigate = useNavigate()

  const prefix = OPERATION_TYPES[type]
  const kind = OPERATION_TARGET_KINDS[type]
  const targetLooksWrong = targetMismatch(type, target)

  const submit = () => {
    create.mutate(
      { type, target: qualifyTarget(type, target), reason: reason || null },
      {
        onSuccess: (operation) => {
          setOpen(false)
          const go = () =>
            navigate({
              to: "/operations/$operationId",
              params: { operationId: operation.id },
            })
          if (stayOnPage) {
            toast.success(`${operation.type} requested`, {
              description: operation.id,
              action: { label: "View", onClick: go },
            })
            return
          }
          toast.success(`${operation.type} requested`, {
            description: operation.id,
          })
          go()
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not start operation")),
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? <Button size="sm">New operation</Button>}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New operation</DialogTitle>
          <DialogDescription>
            An operation is a type and a target. It gets an id and a phase, and
            is polled — it is not a button that returns 200.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="operation-type">Type</Label>
            <Select
              value={type}
              onValueChange={(v) => setType(v as OperationType)}
            >
              <SelectTrigger id="operation-type" className="font-mono text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TYPES.map((value) => (
                  <SelectItem
                    key={value}
                    value={value}
                    className="font-mono text-xs"
                  >
                    {value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="operation-target">Target</Label>
            <Input
              id="operation-target"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder={`${kind}:name or ${prefix}…`}
              className="font-mono text-xs"
            />
            <p
              className={
                targetLooksWrong
                  ? "font-mono text-2xs text-destructive"
                  : "font-mono text-2xs text-muted-foreground/60"
              }
            >
              {type} acts on {kind} targets — an id starting {prefix}, or a name
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="operation-reason">Reason</Label>
            <Input
              id="operation-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="firmware"
              className="font-mono text-xs"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={target === "" || targetLooksWrong || create.isPending}
          >
            {create.isPending ? "Starting…" : "Start operation"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
