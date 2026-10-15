import {
  FormDialog,
  FormGrid,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { Badge } from "@/components/ui/badge"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { formatError } from "@/lib/format"
import { useAllPages } from "@/lib/paginate"
import { cn } from "@/lib/utils"
import {
  BENCHMARK_LEVELS,
  BENCHMARK_MODES,
  BENCHMARK_TIERS,
  LEVEL_CLAIM,
  STEPPED_MODES,
} from "@/modules/benchmarks/display"
import {
  RUN_KINDS,
  RUN_KIND_ORDER,
  runKind,
  type RunKind,
} from "@/modules/benchmarks/kinds"
import {
  useCreateBenchmarkRequest,
  useHarnesses,
} from "@/modules/benchmarks/queries"
import type {
  BenchmarkHarness,
  BenchmarkLevel,
  BenchmarkMode,
  BenchmarkParameter,
  BenchmarkTier,
} from "@/modules/benchmarks/types"
import {
  fetchDeployments,
  fetchEndpoints,
  fetchWorkloads,
} from "@/modules/demand/api"
import { IconChevronRight } from "@tabler/icons-react"
import { useNavigate } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"

const NO_TIER = "__none__"

/**
 * The three collections a request can be aimed at, drained one at a time.
 *
 * Only the selected level's collection is fetched: opening this dialog should
 * not walk every deployment, workload and endpoint in the fleet to fill in two
 * lists nobody is going to look at.
 */
function useTargetOptions(level: BenchmarkLevel) {
  const deployments = useAllPages(
    ["deployments", "options"],
    (cursor) => fetchDeployments({}, cursor, 200),
    { enabled: level === "deployment" }
  )
  const workloads = useAllPages(
    ["workloads", "options"],
    (cursor) => fetchWorkloads({}, cursor, 200),
    { enabled: level === "workload" }
  )
  const endpoints = useAllPages(
    ["endpoints", "options"],
    (cursor) => fetchEndpoints({}, cursor, 200),
    { enabled: level === "endpoint" }
  )

  return useMemo(() => {
    if (level === "workload")
      return (workloads.data ?? []).map((workload) => ({
        value: workload.id,
        label: workload.name,
      }))
    if (level === "endpoint")
      return (endpoints.data ?? []).map((endpoint) => ({
        value: endpoint.id,
        label: endpoint.name,
      }))
    return (deployments.data ?? []).map((deployment) => ({
      value: deployment.id,
      label: `${deployment.name} · ${deployment.cluster_id}`,
    }))
  }, [level, deployments.data, workloads.data, endpoints.data])
}

/** `1000, 8000, 24000` — the load points a stepped mode walks, in order. */
function parseSteps(text: string): number[] {
  return text
    .split(/[,\s]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map(Number)
    .filter((value) => Number.isFinite(value) && value > 0)
}

/**
 * `POST /v1/benchmarks/requests` — the ask, which is declared state.
 *
 * Kind first, then target, then the knobs. The old order opened on a harness
 * name, which asks the reader to already know that `evalscope` grades answers
 * and `aiperf` times them; the question each one answers is the thing somebody
 * arrives holding, and the harness is an implementation detail of it. A kind
 * with one registered harness never shows a harness field at all.
 *
 * Everything below the target is collapsed by default. A run started from here
 * with nothing opened is the harness's own defaults against the plane's current
 * standard, which is the right run to take nine times out of ten.
 */
export function NewRunDialog({
  open,
  onOpenChange,
  kind: initialKind,
  level: initialLevel,
  target: initialTarget,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  kind?: RunKind
  level?: BenchmarkLevel
  target?: string
}) {
  const [kind, setKind] = useState<RunKind | undefined>(initialKind)
  const [level, setLevel] = useState<BenchmarkLevel>(
    initialLevel ?? "deployment"
  )
  const [target, setTarget] = useState(initialTarget ?? "")
  const [harness, setHarness] = useState("")
  const [mode, setMode] = useState<BenchmarkMode>("single")
  const [steps, setSteps] = useState("")
  const [criteria, setCriteria] = useState("")
  const [tier, setTier] = useState(NO_TIER)
  const [image, setImage] = useState("")
  const [reason, setReason] = useState("")
  const [values, setValues] = useState<Record<string, string | boolean>>({})

  const { data: catalogue, isLoading } = useHarnesses()
  const create = useCreateBenchmarkRequest()
  const navigate = useNavigate()
  const harnesses = useMemo(() => catalogue?.items ?? [], [catalogue])

  /** Only the kinds something is actually registered under is offerable. */
  const kinds = useMemo(() => {
    const present = new Set(harnesses.map((entry) => runKind(entry.name)))
    return RUN_KIND_ORDER.filter((entry) => present.has(entry))
  }, [harnesses])

  const ofKind = useMemo(
    () => harnesses.filter((entry) => runKind(entry.name) === kind),
    [harnesses, kind]
  )

  const selected: BenchmarkHarness | undefined =
    ofKind.find((entry) => entry.name === harness) ?? ofKind[0]

  const levels = selected?.levels?.length
    ? BENCHMARK_LEVELS.filter((entry) => selected.levels.includes(entry))
    : BENCHMARK_LEVELS

  /**
   * Derived rather than corrected in an effect. A harness that does not grade
   * at the level currently selected — a probe suite when `workload` was picked
   * for a ramp — reads as its first declared level immediately, instead of
   * rendering one frame of an impossible request and then rewriting itself.
   */
  const effectiveLevel = levels.includes(level) ? level : levels[0]

  const targets = useTargetOptions(effectiveLevel)

  const chooseKind = (next: RunKind) => {
    setKind(next)
    setHarness("")
    setValues({})
    setTarget("")
  }

  const parameters = (selected?.parameters ?? []).filter(
    (parameter) =>
      parameter.levels.length === 0 || parameter.levels.includes(effectiveLevel)
  )

  const knob = STEPPED_MODES[mode]
  const parsedSteps = parseSteps(steps)

  const valueOf = (parameter: BenchmarkParameter): string | boolean =>
    values[parameter.name] ?? (parameter.default as string | boolean)

  const set = (name: string, value: string | boolean) =>
    setValues((current) => ({ ...current, [name]: value }))

  const submit = () => {
    if (!selected) return
    // Only what was actually changed is sent, so a later change to a harness
    // default still applies to anything started from here.
    const body: Record<string, unknown> = {}
    for (const parameter of parameters) {
      const value = values[parameter.name]
      if (value === undefined) continue
      if (value === "" && parameter.type !== "str") continue
      body[parameter.name] = value
    }

    create.mutate(
      {
        level: effectiveLevel,
        target,
        harness: selected.name,
        image: image.trim() || null,
        mode,
        steps: knob ? parsedSteps : [],
        criteria: criteria.trim() || null,
        tier: tier === NO_TIER ? null : (tier as BenchmarkTier),
        reason: reason.trim() || null,
        parameters: body,
      },
      {
        onSuccess: (request) => {
          onOpenChange(false)
          toast.success(`${request.level} request created`, {
            description: `${request.runs.length} run${request.runs.length === 1 ? "" : "s"} · ${request.criteria_version}`,
          })
          navigate({
            to: "/workbench/requests/$requestId",
            params: { requestId: request.id },
          })
        },
        onError: (error) =>
          toast.error(formatError(error, "Could not create benchmark request")),
      }
    )
  }

  return (
    <FormDialog
      icon="benchmark"
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="New run"
      description="What to ask, of what, and against which frozen standard. The plane expands it into runs."
      submitLabel="Run"
      error={create.error}
      pending={create.isPending}
      disabled={!selected || !target || (!!knob && parsedSteps.length === 0)}
      onSubmit={submit}
    >
      <div className="space-y-1.5">
        <Label className="font-mono text-2xs">Type</Label>
        <div className="grid gap-2 sm:grid-cols-2">
          {kinds.map((entry) => (
            <KindCard
              key={entry}
              kind={entry}
              count={
                harnesses.filter((item) => runKind(item.name) === entry).length
              }
              selected={kind === entry}
              onSelect={() => chooseKind(entry)}
            />
          ))}
        </div>
        {isLoading && (
          <p className="text-2xs text-muted-foreground">
            Loading the harness catalogue…
          </p>
        )}
      </div>

      {selected && (
        <>
          {ofKind.length > 1 && (
            <SelectField
              label="Harness"
              value={selected.name}
              onChange={setHarness}
              hint={selected.description}
              options={ofKind.map((entry) => ({
                value: entry.name,
                label: entry.name,
              }))}
            />
          )}

          <FormGrid>
            <SelectField
              label="Level"
              value={effectiveLevel}
              onChange={(value) => {
                setLevel(value as BenchmarkLevel)
                setTarget("")
              }}
              hint={`Measures ${LEVEL_CLAIM[effectiveLevel]}. A result at one level is never comparable with a result at another.`}
              options={levels.map((entry) => ({ value: entry, label: entry }))}
            />
            <SelectField
              label="Target"
              value={target}
              onChange={setTarget}
              placeholder={`Select a ${effectiveLevel}…`}
              options={targets}
            />
          </FormGrid>

          <Advanced label="Shape">
            <FormGrid>
              <SelectField
                label="Mode"
                value={mode}
                onChange={(value) => setMode(value as BenchmarkMode)}
                hint={
                  knob
                    ? `Walks ${knob}, one run per step.`
                    : "One run, no steps."
                }
                options={BENCHMARK_MODES.map((entry) => ({
                  value: entry,
                  label: entry,
                }))}
              />
              {knob && (
                <TextField
                  label="Steps"
                  value={steps}
                  onChange={setSteps}
                  placeholder="1000, 8000, 24000"
                  hint={`Comma separated ${knob} values. A sweep is what makes a per-band claim evidenceable — one run measures one load point.`}
                />
              )}
            </FormGrid>

            {parameters.length > 0 && (
              <div className="grid gap-3 sm:grid-cols-2">
                {parameters.map((parameter) => (
                  <div
                    key={parameter.name}
                    className={
                      parameter.name === "extra"
                        ? "space-y-1.5 sm:col-span-2"
                        : "space-y-1.5"
                    }
                  >
                    <Label
                      htmlFor={`req-${parameter.name}`}
                      className="font-mono text-2xs"
                    >
                      {parameter.name}
                    </Label>
                    {parameter.type === "bool" ? (
                      <div className="flex h-9 items-center">
                        <Switch
                          id={`req-${parameter.name}`}
                          checked={valueOf(parameter) as boolean}
                          onCheckedChange={(value) =>
                            set(parameter.name, value)
                          }
                        />
                      </div>
                    ) : (
                      <Input
                        id={`req-${parameter.name}`}
                        className="font-mono text-xs"
                        inputMode={
                          parameter.type === "int" ? "numeric" : "text"
                        }
                        value={String(valueOf(parameter))}
                        onChange={(event) =>
                          set(parameter.name, event.target.value)
                        }
                      />
                    )}
                    <p className="text-2xs text-muted-foreground">
                      {parameter.description}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </Advanced>

          <Advanced label="Provenance">
            <FormGrid>
              <SelectField
                label="Service tier"
                value={tier}
                onChange={setTier}
                hint="Unstated grades inconclusive rather than lenient — the tier is not derivable from anything here."
                options={[
                  { value: NO_TIER, label: "unstated" },
                  ...BENCHMARK_TIERS.map((entry) => ({
                    value: entry,
                    label: entry,
                  })),
                ]}
              />
              <TextField
                label="Criteria version"
                value={criteria}
                onChange={setCriteria}
                placeholder="the plane's current standard"
                hint="Pinned on the request. Verdicts are derived against it and never stored."
              />
            </FormGrid>
            <TextField
              label="Image"
              value={image}
              onChange={setImage}
              placeholder={selected.image ?? "the harness's pinned image"}
              hint="Leave empty to follow the pin. A run on another image is its own comparison group."
            />
            <TextField
              label="Reason"
              value={reason}
              onChange={setReason}
              mono={false}
              placeholder="why this measurement is being taken"
            />
          </Advanced>
        </>
      )}
    </FormDialog>
  )
}

/**
 * One question, and how many harnesses answer it.
 *
 * The question is the label's whole job. "Conformance" and "Evaluation" are
 * near-synonyms in English and opposite claims here, and a card that shows only
 * the noun makes the reader guess which is which every single time.
 */
function KindCard({
  kind,
  count,
  selected,
  onSelect,
}: {
  kind: RunKind
  count: number
  selected: boolean
  onSelect: () => void
}) {
  const meta = RUN_KINDS[kind]
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-3 text-left transition-colors",
        selected
          ? "border-primary/50 bg-primary/5"
          : "hover:border-muted-foreground/30 hover:bg-muted/40"
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{meta.label}</span>
        {count > 1 && (
          <Badge variant="outline" className="font-mono text-2xs">
            {count}
          </Badge>
        )}
      </div>
      <span className="text-2xs leading-snug text-muted-foreground">
        {meta.question}
      </span>
    </button>
  )
}

/** A section that is closed until somebody has a reason to open it. */
function Advanced({
  label,
  children,
}: React.PropsWithChildren<{ label: string }>) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex w-full items-center gap-1.5 border-t pt-3 font-mono text-2xs tracking-[0.12em] text-muted-foreground uppercase hover:text-foreground">
        <IconChevronRight
          className={cn("size-3 transition-transform", open && "rotate-90")}
        />
        {label}
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-4 pt-3">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}
