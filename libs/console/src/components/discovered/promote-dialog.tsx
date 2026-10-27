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
import {
  usePriorityClassOptions,
  useProjectOptions,
} from "@/modules/binding/queries"
import { usePromoteWorkload } from "@/modules/discovered/queries"
import { workloadLabel } from "@/modules/discovered/display"
import type { DiscoveredWorkload } from "@/modules/discovered/types"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"
import { toast } from "sonner"

const NONE = "__none__"

/**
 * Promotion is the one decision here that produces declared state, so it asks for
 * the two things the plane cannot infer — which weights these are, and who owns
 * it — and nothing else. How the workload is served is read off what is running
 * instead of picked from a list: there is no catalog of serving configurations
 * to pick from, and the cluster's own spec is a better answer than a person
 * matching one by eye.
 *
 * Replica bounds are taken from the live workload rather than offered as a field:
 * a promotion that also set scale would rescale a serving model as a side effect
 * of being cataloged.
 *
 * The fingerprint is sent back exactly as it was shown. If the pod template moved
 * since this row was rendered the plane refuses, which is the point.
 */
export function PromoteDialog({
  workload,
  trigger,
  open: controlledOpen,
  onOpenChange,
  artifact,
}: {
  workload: DiscoveredWorkload
  trigger?: React.ReactNode
  open?: boolean
  onOpenChange?: (open: boolean) => void
  /**
   * The weights, resolved by the adoption flow's first step. The plane reads
   * everything else off the sighting, so this is the whole of what a promotion
   * states about the configuration.
   */
  artifact?: string
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false)
  const open = controlledOpen ?? uncontrolledOpen
  const setOpen = (next: boolean) => {
    setUncontrolledOpen(next)
    onOpenChange?.(next)
  }
  const [project, setProject] = useState(workload.project_id ?? "")
  const [name, setName] = useState(workload.name)
  const [priorityClass, setPriorityClass] = useState(NONE)

  const { data: projects } = useProjectOptions()
  const { data: priorityClasses } = usePriorityClassOptions()
  const promote = usePromoteWorkload()
  const navigate = useNavigate()

  const fingerprint = workload.spec_fingerprint
  const ready = project !== "" && !!artifact && !!fingerprint

  const submit = () => {
    if (!fingerprint) return
    promote.mutate(
      {
        ref: workload.id,
        body: {
          artifact: artifact!,
          project,
          fingerprint,
          name: name || null,
          priority_class: priorityClass === NONE ? null : priorityClass,
        },
      },
      {
        onSuccess: (deployment) => {
          setOpen(false)
          toast.success(`Adopted ${workloadLabel(workload)}`, {
            description: deployment.id,
            action: {
              label: "View",
              onClick: () => navigate({ to: "/deployments" }),
            },
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not promote workload")),
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {controlledOpen === undefined && (
        <DialogTrigger asChild>
          {trigger ?? (
            <Button size="sm" variant="outline">
              Promote
            </Button>
          )}
        </DialogTrigger>
      )}
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="break-all">
            Promote {workloadLabel(workload)}
          </DialogTitle>
          <DialogDescription>
            Adopting this workload makes it a deployment the plane manages. It
            keeps the replicas and the weights it already has; from here the
            cluster refuses to reconcile it if what is running stops matching
            what was adopted.
          </DialogDescription>
        </DialogHeader>

        <div className="min-w-0 space-y-4">
          <div className="space-y-1.5 rounded-md border bg-muted/30 p-3">
            <p className="font-mono text-2xs text-muted-foreground">
              {artifact
                ? "the serving block is read off what this cluster is running — only the weights are declared"
                : "no weights identified yet — register them from the reading first"}
            </p>
            <p className="font-mono text-2xs break-all text-muted-foreground">
              running {workload.images.join(", ") || "an unrecorded image"}
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="promote-project">Project</Label>
            <Select value={project} onValueChange={setProject}>
              <SelectTrigger id="promote-project" className="font-mono text-xs">
                <SelectValue placeholder="Who owns it" />
              </SelectTrigger>
              <SelectContent>
                {(projects ?? []).map((option) => (
                  <SelectItem
                    key={option.id}
                    value={option.id}
                    className="font-mono text-xs"
                  >
                    {option.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="promote-name">Deployment name</Label>
            <Input
              id="promote-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="font-mono text-xs"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="promote-priority">Priority class</Label>
            <Select value={priorityClass} onValueChange={setPriorityClass}>
              <SelectTrigger
                id="promote-priority"
                className="font-mono text-xs"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE} className="font-mono text-xs">
                  project default
                </SelectItem>
                {(priorityClasses ?? []).map((option) => (
                  <SelectItem
                    key={option.id}
                    value={option.name}
                    className="font-mono text-xs"
                  >
                    {option.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="rounded-md border bg-muted/30 p-3">
            <p className="font-mono text-2xs text-muted-foreground">
              scale is taken from the workload — {workload.replicas_desired}{" "}
              replica{workload.replicas_desired === 1 ? "" : "s"},{" "}
              {workload.devices_per_replica} device
              {workload.devices_per_replica === 1 ? "" : "s"} each
            </p>
            <p className="mt-1 font-mono text-2xs break-all text-muted-foreground">
              fingerprint {fingerprint ?? "unknown — cannot promote"}
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!ready || promote.isPending}>
            {promote.isPending ? "Promoting…" : "Promote"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
