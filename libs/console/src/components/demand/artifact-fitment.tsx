import { RefreshSourceButton } from "@/components/demand/artifact-refresh"
import { Dash } from "@/components/layout/mono"
import { StatusDot } from "@/components/layout/status-dot"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useArtifactFitment } from "@/modules/demand/queries"
import type {
  Artifact,
  ArtifactFit,
  FitmentScope,
  RecipeFootprint,
} from "@/modules/demand/types"
import { useState } from "react"

/**
 * The scope switch is labelled by the question, not by the filter.
 *
 * `fleet` and `catalog` are two different questions that happen to be answered with
 * the same table, and calling them "our hardware" and "all hardware" made the second
 * read as a wider view of the first. It is not: a fit on a card we own none of is a
 * procurement answer, and nothing in the second list is deployable today.
 */
const SCOPES: { value: FitmentScope; label: string; caption: string }[] = [
  {
    value: "fleet",
    label: "What we can run",
    caption:
      "Only classes a cluster has reported. A row that does not fit here is a dead end.",
  },
  {
    value: "catalog",
    label: "What we'd need",
    caption:
      "Every class we know how to name. A fit on hardware we own none of is what to go and buy.",
  },
]

const CONCURRENCIES = [1, 8, 32, 128, 256]

/**
 * Quantizations worth asking about, not the whole of `BYTES_PER_PARAM`.
 *
 * The empty value means "however the weights ship", which is the plane's default and
 * the only honest starting point — the artifact already knows its own precision, and
 * offering fp16 as the default would silently oversize a repo that ships mxfp4.
 */
const QUANTIZATIONS = ["", "fp16", "fp8", "int8", "int4", "mxfp4", "awq"]

/** Halvings down from the artifact's declared window, which is the plane's default. */
function contextChoices(declared: number | null): number[] {
  if (!declared || declared < 4096) return [8192, 32768, 131072]
  const choices: number[] = []
  for (let value = declared; value >= 4096; value = Math.floor(value / 2))
    choices.push(value)
  return choices
}

function formatTokens(value: number): string {
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}k`
  return value.toLocaleString()
}

/**
 * A fit is three states, not two.
 *
 * `reduced` is a fit that had to cut the window, and flattening it into a failure
 * would hide the cheapest card that works from anyone willing to serve at 32k.
 */
function fitTone(fit: ArtifactFit) {
  if (!fit.fits) return { tone: "neutral" as const, text: "no" }
  if (!fit.kv_counted) return { tone: "info" as const, text: "holds" }
  if (fit.reduced)
    return {
      tone: "warn" as const,
      text: `at ${formatTokens(fit.context_length)}`,
    }
  return { tone: "ok" as const, text: "fits" }
}

/**
 * How much of the usable VRAM the weights take and how much the whole configuration
 * needs, as one bar. Two numbers rather than one because the gap between them is the KV
 * cache and the activation peak, which is the part that moves when somebody changes the
 * window or the stream count.
 *
 * "usable" is not the nameplate figure. It is net of the engine's utilization envelope
 * and a per-device CUDA reserve, so a 2x80GB row reads as 141 rather than 160 — the
 * number a serving process can actually have. Showing the nameplate would over-promise
 * by about fifteen percent, which is a card count at the boundary.
 */
function MemoryBar({ fit }: { fit: ArtifactFit }) {
  const pct = (value: number) =>
    fit.aggregate_gb > 0
      ? `${Math.min(100, (value / fit.aggregate_gb) * 100)}%`
      : "0%"
  return (
    <div className="flex min-w-32 flex-col gap-1">
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="absolute inset-y-0 left-0 bg-muted-foreground/30"
          style={{ width: pct(fit.required_gb) }}
        />
        <div
          className={cn(
            "absolute inset-y-0 left-0",
            fit.fits ? "bg-primary/70" : "bg-destructive/70"
          )}
          style={{ width: pct(fit.weights_gb) }}
        />
      </div>
      <span className="font-mono text-2xs text-muted-foreground/70 tabular-nums">
        {fit.kv_counted ? (
          <>
            {Math.round(fit.weights_gb)} +{" "}
            {Math.round(fit.required_gb - fit.weights_gb)} of{" "}
            {Math.round(fit.aggregate_gb)} usable
          </>
        ) : (
          <>
            {Math.round(fit.weights_gb)}+ of {Math.round(fit.aggregate_gb)}{" "}
            usable
          </>
        )}
      </span>
    </div>
  )
}

/**
 * What we own of a class, which is a different question from whether it fits.
 *
 * Not a placement verdict: allocatable devices say nothing about whether
 * `device_count` of them share one topology domain, and placement preview is the
 * only thing that can answer that. Zero allocatable against a non-zero total is a
 * capacity problem that waiting fixes; zero total is a purchasing one.
 */
function FleetCell({ fit }: { fit: ArtifactFit }) {
  if (fit.devices_total === 0)
    return (
      <span className="font-mono text-2xs text-muted-foreground/40">none</span>
    )
  const short = fit.fits && fit.devices_allocatable < fit.device_count
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "font-mono text-2xs tabular-nums",
            short && "text-amber-500"
          )}
        >
          {fit.devices_allocatable}/{fit.devices_total}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {fit.devices_allocatable} of {fit.devices_total} allocatable across{" "}
        {fit.cluster_count} cluster{fit.cluster_count === 1 ? "" : "s"}
        {short && ` — fewer free than the ${fit.device_count} a replica needs`}
      </TooltipContent>
    </Tooltip>
  )
}

function Terms({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      {children}
    </div>
  )
}

/**
 * The device classes an artifact could be served on, and the entry point to
 * deploying it.
 *
 * Deliberately not a form. The plane derives every number here from the weights, and
 * the terms along the top are the only things a person has to state — so the panel
 * reads as a proposal to poke at rather than a set of questions to answer. Picking a
 * row is picking a footprint, which is which card and how many, and nothing else.
 */
/**
 * Two different questions wearing one table.
 *
 * `explore` is the artifact page: what could these weights run on, including hardware we
 * do not own, and each row offers to go and deploy it. Nothing is selected, because
 * nothing is being assembled — a row is a link out.
 *
 * `select` is step two of the deploy flow, where the table is a control. A row is not an
 * action there, it is the answer to "which device class and how many", so it selects and
 * stays selected. Procurement is not on the menu either: the "what we'd need" scope asks
 * what to go and buy, and a deploy that cannot happen on hardware we own is not a row
 * worth offering halfway through creating a workload.
 */
export type FitmentMode = "explore" | "select"

export function ArtifactFitmentPanel({
  artifact,
  onPick,
  bare = false,
  mode = "explore",
  selected = null,
  actionLabel = "Deploy",
}: {
  artifact: Artifact
  /**
   * The row's own fit comes with the footprint, because a caller that has to say what
   * the fleet holds of the chosen class would otherwise have to find the row again.
   */
  onPick?: (footprint: RecipeFootprint, fit: ArtifactFit) => void
  /**
   * Drop the card chrome and the title, for a caller that is already a card and has
   * already asked the question. Nested cards read as a box inside a box, and inside the
   * deploy flow the step heading has said "how to serve it" — repeating "Fitment"
   * underneath it names the mechanism where the question used to be.
   */
  bare?: boolean
  mode?: FitmentMode
  /** Device class of the chosen row, in `select` mode. */
  selected?: string | null
  /** The per-row button's words, in `explore` mode. */
  actionLabel?: string
}) {
  const declared = (artifact.metadata?.context_length ?? null) as number | null
  const choices = contextChoices(declared)
  const selecting = mode === "select"
  const [scope, setScope] = useState<FitmentScope>("fleet")
  const [contextLength, setContextLength] = useState<number | undefined>()
  const [concurrency, setConcurrency] = useState(1)
  const [quantization, setQuantization] = useState("")
  const [everything, setEverything] = useState(false)

  /** Selecting is always against the fleet: the other scope answers a purchase order. */
  const effectiveScope: FitmentScope = selecting ? "fleet" : scope
  const { data, isLoading, error } = useArtifactFitment(artifact.id, {
    scope: effectiveScope,
    concurrency,
    ...(contextLength ? { context_length: contextLength } : {}),
    ...(quantization ? { quantization } : {}),
    ...(everything ? { limit: 0 } : {}),
  })
  const active = SCOPES.find((entry) => entry.value === effectiveScope)

  const header = (
    <>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          {!bare && <CardTitle className="text-sm">Fitment</CardTitle>}
          <p className="font-mono text-2xs text-muted-foreground/60">
            {active?.caption}
          </p>
        </div>
        {!selecting && (
          <Tabs
            value={scope}
            onValueChange={(v) => setScope(v as FitmentScope)}
          >
            <TabsList>
              {SCOPES.map((entry) => (
                <TabsTrigger key={entry.value} value={entry.value}>
                  {entry.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-4 border-t pt-3">
        <Terms label="Context">
          <Select
            value={String(contextLength ?? data?.context_length ?? "")}
            onValueChange={(v) => setContextLength(Number(v))}
          >
            <SelectTrigger size="sm" className="h-7 w-24 font-mono text-2xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {choices.map((value) => (
                <SelectItem key={value} value={String(value)}>
                  {value.toLocaleString()}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Terms>
        <Terms label="Streams">
          <Select
            value={String(concurrency)}
            onValueChange={(v) => setConcurrency(Number(v))}
          >
            <SelectTrigger size="sm" className="h-7 w-16 font-mono text-2xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CONCURRENCIES.map((value) => (
                <SelectItem key={value} value={String(value)}>
                  {value}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Terms>
        <Terms label="Quantization">
          <Select
            value={quantization}
            onValueChange={(v) => setQuantization(v === "native" ? "" : v)}
          >
            <SelectTrigger size="sm" className="h-7 w-28 font-mono text-2xs">
              <SelectValue placeholder="as shipped" />
            </SelectTrigger>
            <SelectContent>
              {QUANTIZATIONS.map((value) => (
                <SelectItem key={value || "native"} value={value || "native"}>
                  {value || "as shipped"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Terms>
        {data && (
          <span className="ml-auto font-mono text-2xs text-muted-foreground/50">
            sized at {data.quantization}
          </span>
        )}
      </div>
    </>
  )

  const content = (
    <>
      {isLoading && <Skeleton className="h-40 w-full" />}
      {error && (
        <p className="text-xs text-destructive">
          {formatError(error, "Could not fit this artifact")}
        </p>
      )}
      {data && !data.sizable && (
        <div className="space-y-2">
          <p className="font-mono text-2xs text-muted-foreground/60">
            {data.note}
          </p>
          <RefreshSourceButton
            artifact={artifact}
            size="sm"
            variant="secondary"
          />
        </div>
      )}
      {data?.sizable && data.items.length === 0 && (
        <p className="font-mono text-2xs text-muted-foreground/60">
          {data.note}
        </p>
      )}
      {data?.sizable && data.items.length > 0 && data.note && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2.5">
          <p className="font-mono text-2xs text-amber-600 dark:text-amber-500">
            {data.note}
          </p>
          {/* The note ends by saying re-resolving the source prices the window. This is that. */}
          {data.items.every((item) => !item.kv_counted) && (
            <RefreshSourceButton
              artifact={artifact}
              size="sm"
              variant="secondary"
            />
          )}
        </div>
      )}
      {data?.sizable && data.items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
                {selecting && <th className="w-6 p-2" />}
                <th className="p-2 text-left font-medium">Device class</th>
                <th className="p-2 text-left font-medium">Fit</th>
                <th className="p-2 text-left font-medium">Hardware</th>
                <th className="p-2 text-left font-medium">
                  {data.items.some((item) => item.kv_counted)
                    ? "Memory"
                    : "Weights"}
                </th>
                <th className="p-2 text-left font-medium">Free</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((fit) => (
                <FitRow
                  key={fit.device_class}
                  fit={fit}
                  quantization={data.quantization}
                  onPick={onPick}
                  selecting={selecting}
                  selected={selected === fit.device_class}
                  actionLabel={actionLabel}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data?.sizable && data.considered > data.items.length && (
        <button
          type="button"
          onClick={() => setEverything(true)}
          className="mt-2 font-mono text-2xs text-muted-foreground/60 hover:text-primary"
        >
          showing the top {data.items.length} of {data.considered} ranked — show
          all
        </button>
      )}
      {data?.sizable && everything && data.items.length > 5 && (
        <button
          type="button"
          onClick={() => setEverything(false)}
          className="mt-2 font-mono text-2xs text-muted-foreground/60 hover:text-primary"
        >
          showing all {data.considered} — show the top 5
        </button>
      )}
    </>
  )

  if (bare)
    return (
      <div className="space-y-3">
        {header}
        {content}
      </div>
    )

  return (
    <Card>
      <CardHeader className="gap-3 pb-2">{header}</CardHeader>
      <CardContent>{content}</CardContent>
    </Card>
  )
}

function FitRow({
  fit,
  quantization,
  onPick,
  selecting = false,
  selected = false,
  actionLabel = "Deploy",
}: {
  fit: ArtifactFit
  quantization: string
  onPick?: (footprint: RecipeFootprint, fit: ArtifactFit) => void
  selecting?: boolean
  selected?: boolean
  actionLabel?: string
}) {
  const state = fitTone(fit)
  const pick = () =>
    onPick?.(
      {
        device_class: fit.device_class,
        device_count: fit.device_count,
        tensor_parallel: fit.tensor_parallel,
        pipeline_parallel: fit.pipeline_parallel,
        context_length: fit.context_length,
        quantization,
      },
      fit
    )
  /**
   * The whole row is the target when selecting. A button in the last cell is right for
   * an action — it is a thing you do — but wrong for a choice among five, where the
   * clickable area should be the thing you are comparing.
   */
  const choosable = selecting && fit.fits && !!onPick
  return (
    <tr
      onClick={choosable ? pick : undefined}
      className={cn(
        "border-b last:border-0",
        !fit.fits && "opacity-55",
        choosable && "cursor-pointer hover:bg-primary/5",
        selected && "bg-primary/10 hover:bg-primary/10"
      )}
    >
      {selecting && (
        <td className="pl-2">
          <span
            className={cn(
              "flex size-3.5 items-center justify-center rounded-full border",
              selected
                ? "border-primary bg-primary"
                : fit.fits
                  ? "border-muted-foreground/40"
                  : "border-muted-foreground/20"
            )}
          >
            {selected && (
              <span className="size-1.5 rounded-full bg-primary-foreground" />
            )}
          </span>
        </td>
      )}
      <td className="p-2">
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-xs">{fit.label}</span>
          {!fit.in_catalog && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" className="font-mono text-2xs">
                  observed
                </Badge>
              </TooltipTrigger>
              <TooltipContent>
                Hardware the contract device table does not name. Fitted against
                the memory a cluster reported for it.
              </TooltipContent>
            </Tooltip>
          )}
        </div>
        <span className="font-mono text-2xs text-muted-foreground/50">
          {fit.device_class}
        </span>
      </td>
      <td className="p-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-flex items-center gap-1.5">
              <StatusDot tone={state.tone} />
              <span className="font-mono text-2xs">{state.text}</span>
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-80">{fit.reason}</TooltipContent>
        </Tooltip>
      </td>
      <td className="p-2">
        {fit.fits ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="flex flex-col">
                <span className="font-mono text-xs tabular-nums">
                  {fit.node_count > 1
                    ? `${fit.node_count} nodes x ${Math.min(fit.devices_per_node, fit.device_count)}`
                    : `1 node x ${fit.device_count}`}
                </span>
                <span className="font-mono text-2xs text-muted-foreground/50 tabular-nums">
                  {fit.device_count} dev · tp{fit.tensor_parallel}/pp
                  {fit.pipeline_parallel}
                </span>
              </div>
            </TooltipTrigger>
            <TooltipContent className="max-w-80">
              {fit.node_count > 1
                ? `${fit.node_count} nodes of ${Math.min(fit.devices_per_node, fit.device_count)}, on one backend fabric — tensor-parallel ${fit.tensor_parallel} inside each node, pipeline-parallel ${fit.pipeline_parallel} across them.`
                : `One node of ${fit.device_count}, tensor-parallel ${fit.tensor_parallel}. A node of this class holds ${fit.devices_per_node} devices.`}
            </TooltipContent>
          </Tooltip>
        ) : (
          <Dash>{null}</Dash>
        )}
      </td>
      <td className="p-2">
        <MemoryBar fit={fit} />
      </td>
      <td className="p-2">
        <FleetCell fit={fit} />
      </td>
      <td className="p-2 text-right">
        {fit.fits && onPick && !selecting && (
          <Button size="sm" variant="outline" onClick={pick}>
            {actionLabel}
          </Button>
        )}
      </td>
    </tr>
  )
}
