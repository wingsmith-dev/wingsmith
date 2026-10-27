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
import { Switch } from "@/components/ui/switch"
import { formatError } from "@/lib/format"
import { useCreateBenchmark, useHarnesses } from "@/modules/benchmarks/queries"
import type { BenchmarkParameter } from "@/modules/benchmarks/types"
import { useNavigate } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"

/**
 * `POST /v1/benchmarks` against one deployment.
 *
 * The form is built from `GET /v1/benchmarks/harnesses` rather than from a list
 * of AIPerf flags written out here. The plane validates against that same table,
 * so a field this console does not know about is one it renders anyway, and a
 * flag the plane dropped stops being offered without a console release.
 *
 * `image` is the one field not built from that table. It is left empty to mean
 * "whatever the harness is pinned to", with the pin shown as the placeholder, so
 * the run follows the plane's default unless somebody deliberately types over it
 * — a run started from here should not be pinned to whatever the pin happened to
 * be on the day this dialog was opened.
 *
 * `extra` is rendered as a plain text field and sent verbatim. It is the escape
 * hatch for the long tail of flags, and it is deliberately not validated —
 * enumerating everything a harness accepts, in three languages, is the thing
 * this design is avoiding.
 */
export function NewBenchmarkDialog({
  deployment,
  trigger,
  stayOnPage,
}: {
  deployment: string
  trigger?: React.ReactNode
  stayOnPage?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [harness, setHarness] = useState("aiperf")
  const [image, setImage] = useState("")
  const [reason, setReason] = useState("")
  const [values, setValues] = useState<Record<string, string | boolean>>({})
  const { data: catalog, isLoading } = useHarnesses()
  const create = useCreateBenchmark()
  const navigate = useNavigate()

  const selected = useMemo(() => {
    const items = catalog?.items ?? []
    return items.find((h) => h.name === harness) ?? items[0]
  }, [catalog, harness])
  const harnesses = catalog?.items ?? []

  const valueOf = (parameter: BenchmarkParameter): string | boolean =>
    values[parameter.name] ?? (parameter.default as string | boolean)

  const set = (name: string, value: string | boolean) =>
    setValues((current) => ({ ...current, [name]: value }))

  const submit = () => {
    if (!selected) return
    // Only what the operator actually changed is sent. Posting every field back
    // would pin this run to the defaults as they are today, so a later change to
    // a harness default would silently not apply to anything started from here.
    const parameters: Record<string, unknown> = {}
    for (const parameter of selected.parameters) {
      const value = values[parameter.name]
      if (value === undefined) continue
      if (value === "" && parameter.type !== "str") continue
      parameters[parameter.name] = value
    }

    create.mutate(
      {
        deployment,
        harness: selected.name,
        image: image.trim() || null,
        reason: reason || null,
        parameters,
      },
      {
        onSuccess: (run) => {
          setOpen(false)
          const go = () =>
            navigate({
              to: "/workbench/$runId",
              params: { runId: run.id },
            })
          toast.success(`${run.harness} run started`, {
            description: run.id,
            action: stayOnPage ? { label: "View", onClick: go } : undefined,
          })
          if (!stayOnPage) go()
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not start benchmark")),
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? <Button size="sm">Run benchmark</Button>}
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Run benchmark</DialogTitle>
          <DialogDescription>
            The load is generated inside the cluster and aimed at its own
            frontend. Results are stored in the harness's own shape — two runs
            compare when the harness, its version and its image match.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="benchmark-harness">Harness</Label>
            <Select value={harness} onValueChange={setHarness}>
              <SelectTrigger
                id="benchmark-harness"
                className="font-mono text-xs"
              >
                <SelectValue placeholder={isLoading ? "Loading…" : "Select"} />
              </SelectTrigger>
              <SelectContent>
                {harnesses.map((option) => (
                  <SelectItem
                    key={option.name}
                    value={option.name}
                    className="font-mono text-xs"
                  >
                    {option.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selected && (
              <p className="text-2xs text-muted-foreground">
                {selected.description}
              </p>
            )}
          </div>

          {selected && (
            <div className="grid gap-3 sm:grid-cols-2">
              {selected.parameters.map((parameter) => (
                <div
                  key={parameter.name}
                  className={
                    parameter.name === "extra"
                      ? "space-y-1.5 sm:col-span-2"
                      : "space-y-1.5"
                  }
                >
                  <Label
                    htmlFor={`bm-${parameter.name}`}
                    className="font-mono text-2xs"
                  >
                    {parameter.name}
                  </Label>
                  {parameter.type === "bool" ? (
                    <div className="flex h-9 items-center">
                      <Switch
                        id={`bm-${parameter.name}`}
                        checked={valueOf(parameter) as boolean}
                        onCheckedChange={(v) => set(parameter.name, v)}
                      />
                    </div>
                  ) : (
                    <Input
                      id={`bm-${parameter.name}`}
                      className="font-mono text-xs"
                      inputMode={parameter.type === "int" ? "numeric" : "text"}
                      value={String(valueOf(parameter))}
                      onChange={(e) => set(parameter.name, e.target.value)}
                    />
                  )}
                  <p className="text-2xs text-muted-foreground">
                    {parameter.description}
                  </p>
                </div>
              ))}
            </div>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="benchmark-image">Image</Label>
            <Input
              id="benchmark-image"
              className="font-mono text-xs"
              value={image}
              onChange={(e) => setImage(e.target.value)}
              placeholder={selected?.image ?? "the harness's pinned image"}
            />
            <p className="text-2xs text-muted-foreground">
              Leave empty to use the pinned image above. Override it only to
              reach a release the plane has not pinned yet — a run on a
              different image is its own comparison group.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="benchmark-reason">Reason</Label>
            <Input
              id="benchmark-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="why this measurement is being taken"
            />
          </div>

          {create.error && (
            <p className="text-xs text-destructive">
              {formatError(create.error, "Could not start benchmark")}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={submit}
            disabled={create.isPending || !selected}
          >
            {create.isPending ? "Starting…" : "Run"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
