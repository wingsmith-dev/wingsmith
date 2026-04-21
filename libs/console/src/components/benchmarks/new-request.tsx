import {
  FormDialog,
  FormGrid,
  SelectField,
  TextField,
} from "@/components/layout/form"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { formatError } from "@/lib/format"
import { useAllPages } from "@/lib/paginate"
import {
  BENCHMARK_LEVELS,
  BENCHMARK_MODES,
  BENCHMARK_TIERS,
  LEVEL_CLAIM,
  STEPPED_MODES,
} from "@/modules/benchmarks/display"
import {
  useCreateBenchmarkRequest,
  useHarnesses,
} from "@/modules/benchmarks/queries"
import type {
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
 * The request is the editable half of this screen and the only one: what comes
 * back is a set of runs, and nothing about a run is ever offered for edit here.
 *
 * The level is picked before the target because the three are different claims,
 * and the harness's own `levels` narrows the choice — a probe suite has nothing
 * to say about a deployment with no public name, and asking for it is a 400 on
 * the plane rather than a run that measures nothing.
 *
 * `criteria` is left blank by default so the request pins whatever standard the
 * plane currently publishes. Typing a version here grades against an older one
 * deliberately, which is a different and rarer intent.
 */
export function NewBenchmarkRequestDialog({
  open,
  onOpenChange,
  level: initialLevel,
  target: initialTarget,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  level?: BenchmarkLevel
  target?: string
}) {
  const [level, setLevel] = useState<BenchmarkLevel>(
    initialLevel ?? "deployment"
  )
  const [target, setTarget] = useState(initialTarget ?? "")
  const [harness, setHarness] = useState("aiperf")
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
  const targets = useTargetOptions(level)

  const harnesses = useMemo(() => catalogue?.items ?? [], [catalogue])
  const selected = useMemo(
    () => harnesses.find((entry) => entry.name === harness) ?? harnesses[0],
    [harnesses, harness]
  )

  const levels = selected?.levels?.length
    ? BENCHMARK_LEVELS.filter((entry) => selected.levels.includes(entry))
    : BENCHMARK_LEVELS

  const parameters = (selected?.parameters ?? []).filter(
    (parameter) =>
      parameter.levels.length === 0 || parameter.levels.includes(level)
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
        level,
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
            to: "/benchmarks/requests/$requestId",
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
      title="New benchmark request"
      description="What to measure, at which level, and against which frozen standard. The plane expands it into runs."
      submitLabel="Request"
      error={create.error}
      pending={create.isPending}
      disabled={!selected || !target || (!!knob && parsedSteps.length === 0)}
      onSubmit={submit}
    >
      <FormGrid>
        <SelectField
          label="Harness"
          value={selected?.name ?? ""}
          onChange={setHarness}
          placeholder={isLoading ? "Loading…" : "Select…"}
          hint={selected?.description}
          options={harnesses.map((entry) => ({
            value: entry.name,
            label: entry.name,
          }))}
        />
        <SelectField
          label="Level"
          value={level}
          onChange={(value) => {
            setLevel(value as BenchmarkLevel)
            setTarget("")
          }}
          hint={`Measures ${LEVEL_CLAIM[level]}. A result at one level is never comparable with a result at another.`}
          options={levels.map((entry) => ({ value: entry, label: entry }))}
        />
      </FormGrid>

      <SelectField
        label="Target"
        value={target}
        onChange={setTarget}
        placeholder={`Select a ${level}…`}
        options={targets}
      />

      <FormGrid>
        <SelectField
          label="Mode"
          value={mode}
          onChange={(value) => setMode(value as BenchmarkMode)}
          hint={
            knob ? `Walks ${knob}, one run per step.` : "One run, no steps."
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
                    onCheckedChange={(value) => set(parameter.name, value)}
                  />
                </div>
              ) : (
                <Input
                  id={`req-${parameter.name}`}
                  className="font-mono text-xs"
                  inputMode={parameter.type === "int" ? "numeric" : "text"}
                  value={String(valueOf(parameter))}
                  onChange={(event) => set(parameter.name, event.target.value)}
                />
              )}
              <p className="text-2xs text-muted-foreground/60">
                {parameter.description}
              </p>
            </div>
          ))}
        </div>
      )}

      <TextField
        label="Image"
        value={image}
        onChange={setImage}
        placeholder={selected?.image ?? "the harness's pinned image"}
        hint="Leave empty to follow the pin. A run on another image is its own comparison group."
      />

      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        mono={false}
        placeholder="why this measurement is being taken"
      />
    </FormDialog>
  )
}
