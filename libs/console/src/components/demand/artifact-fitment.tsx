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
import { ACTIVATION_FOCUS, activationProps } from "@/lib/keyboard"
import { cn } from "@/lib/utils"
import { useArtifactFitment } from "@/modules/demand/queries"
import type {
  Artifact,
  ArtifactFit,
  ArtifactFitLayout,
  FitmentScope,
  ServingFootprint,
} from "@/modules/demand/types"
import { useState } from "react"

/**
 * The scope switch is labelled by the question, not by the filter.
 *
 * `fleet` and `catalog` are two different questions that happen to be answered with
 * the same table, and labelling them as a narrow and a wide filter would make the
 * second read as a wider view of the first. It is not: a fit on a class the fleet has
 * none of needs hardware first, and nothing in the second list is deployable today.
 */
const SCOPES: { value: FitmentScope; label: string; caption: string }[] = [
  {
    value: "fleet",
    label: "Fleet",
    caption:
      "Only classes a cluster has reported. A row that does not fit here cannot be deployed.",
  },
  {
    value: "catalog",
    label: "Catalog",
    caption:
      "Every known class, including ones no cluster has reported. A fit here needs hardware the fleet does not have.",
  },
]

const CONCURRENCIES = [1, 8, 32, 128, 256]

/**
 * Quantizations worth asking about, not the whole of `BYTES_PER_PARAM`.
 *
 * The empty value means "however the weights ship", which is the plane's default and
 * the only honest starting point — the artifact already knows its own precision, and
 * offering fp16 as the default would silently oversize a repo that ships mxfp4.
 *
 * fp16 is absent on purpose. It is a dtype, so it is the one entry here that could not
 * also be declared as a quantization, and this list feeds both questions: how big is it
 * at this precision, and what should the serving block say. Nothing is lost by dropping
 * it — unquantized weights are already sized at fp16 under the empty value, which is the
 * same arithmetic under the name a serving block can actually carry.
 */
const QUANTIZATIONS = ["", "fp8", "int8", "int4", "mxfp4", "awq"]

/**
 * Halvings down from the artifact's declared window, which is the plane's default.
 *
 * `current` is folded in because this list is not the only way the window gets
 * set. The Params step edits the same value, and a window typed there — or one the
 * plane reduced a fit to — is rarely a halving of the declared maximum. A Select whose
 * value is absent from its own options renders empty, which reads as "no window
 * chosen" on a form that has one.
 */
function contextChoices(
  declared: number | null,
  current?: number | null
): number[] {
  const halvings: number[] = []
  if (!declared || declared < 4096) halvings.push(131072, 32768, 8192)
  else
    for (let value = declared; value >= 4096; value = Math.floor(value / 2))
      halvings.push(value)
  if (current && !halvings.includes(current)) halvings.push(current)
  return [...new Set(halvings)].sort((a, b) => b - a)
}

function formatTokens(value: number): string {
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}k`
  return value.toLocaleString()
}

/**
 * A fit is four states, not two.
 *
 * `supply_short` comes first because this column is read as "can it run", and
 * under the Fleet scope a green dot beside a row needing sixty-four cards when the
 * fleet has five is the one answer that is confidently wrong. The arithmetic still fits;
 * the fleet does not, and the row says which.
 *
 * `reduced` is a fit that had to cut the window, and flattening it into a failure
 * would hide the cheapest card that works from anyone willing to serve at 32k.
 */
function fitTone(fit: ArtifactFit) {
  if (!fit.fits) return { tone: "neutral" as const, text: "no" }
  if (fit.supply_short) return { tone: "warn" as const, text: "short" }
  if (!fit.kv_counted) return { tone: "info" as const, text: "holds" }
  if (fit.reduced)
    return {
      tone: "warn" as const,
      text: `at ${formatTokens(fit.context_length)}`,
    }
  return { tone: "ok" as const, text: "fits" }
}

/** Why a row is short, in the words the fit column is too narrow to carry. */
function shortfall(fit: ArtifactFit): string {
  return `Needs ${fit.device_count} devices; the fleet holds ${fit.devices_total} of this class. It fits on paper and cannot be placed until more are bought.`
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
      <span className="font-mono text-2xs text-muted-foreground tabular-nums">
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
 * How much of a class the fleet has, which is a different question from whether it fits.
 *
 * Not a placement verdict: allocatable devices say nothing about whether
 * `device_count` of them share one topology domain, and placement preview is the
 * only thing that can answer that. Zero allocatable against a non-zero total is a
 * capacity problem that waiting fixes; zero total is a purchasing one.
 */
function FleetCell({ fit }: { fit: ArtifactFit }) {
  if (fit.devices_total === 0)
    return (
      <span className="font-mono text-2xs text-muted-foreground">none</span>
    )
  const short = fit.fits && fit.devices_allocatable < fit.device_count
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "font-mono text-2xs tabular-nums",
            short && "text-warning",
            fit.supply_short && "text-warning"
          )}
        >
          {fit.devices_allocatable}/{fit.devices_total}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {fit.devices_allocatable} of {fit.devices_total} allocatable across{" "}
        {fit.cluster_count} cluster{fit.cluster_count === 1 ? "" : "s"}
        {fit.supply_short
          ? ` — fewer of them in total than the ${fit.device_count} a replica needs`
          : short &&
            ` — fewer free than the ${fit.device_count} a replica needs`}
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
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
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
 * `explore` is the artifact page: what could these weights run on, including hardware the
 * fleet does not have, and each row offers to go and deploy it. Nothing is selected, because
 * nothing is being assembled — a row is a link out.
 *
 * `select` is step two of the deploy flow, where the table is a control. A row is not an
 * action there, it is the answer to "which device class and how many", so it selects and
 * stays selected. The Catalog scope is not on the menu either: it asks what hardware
 * would be needed, and a deploy that cannot happen on the fleet is not a row worth
 * offering halfway through creating a workload.
 */
export type FitmentMode = "explore" | "select"

export function ArtifactFitmentPanel({
  artifact,
  onPick,
  bare = false,
  mode = "explore",
  selected = null,
  actionLabel = "Deploy",
  contextLength: contextLengthProp = null,
  onContextLengthChange,
}: {
  artifact: Artifact
  /**
   * The row's own fit comes with the footprint, because a caller that has to say what
   * the fleet holds of the chosen class would otherwise have to find the row again.
   */
  onPick?: (footprint: ServingFootprint, fit: ArtifactFit) => void
  /**
   * Drop the card chrome and the title, for a caller that is already a card and has
   * already asked the question. Nested cards read as a box inside a box, and inside the
   * deploy flow the step heading has said "how to serve it" — repeating "Fitment"
   * underneath it would name the mechanism instead of the question.
   */
  bare?: boolean
  mode?: FitmentMode
  /**
   * The chosen footprint, in `select` mode. The whole footprint and not just the class,
   * because a class is not the answer on its own — the count is part of it, and a
   * row that highlighted on class alone could not say which of its layouts is picked.
   */
  selected?: ServingFootprint | null
  /** The per-row button's words, in `explore` mode. */
  actionLabel?: string
  /**
   * The window to size against, when the caller owns it.
   *
   * Passing both of these makes the Context select a view of the caller's state rather
   * than a question of its own, and that is the whole of the fix: the deploy flow asks
   * for a window in two places — here and in the Params step — and they were two
   * `useState`s that met nowhere. One of them could be changed while the other went on
   * describing a different model, and neither said which was going to be submitted.
   *
   * Omitting them leaves the panel uncontrolled, which is right for the artifact page:
   * there is no form under it and nothing else to agree with.
   */
  contextLength?: number | null
  onContextLengthChange?: (contextLength: number) => void
}) {
  const declared = (artifact.metadata.context_length ?? null) as number | null
  const selecting = mode === "select"
  const [scope, setScope] = useState<FitmentScope>("fleet")
  const [ownContextLength, setOwnContextLength] = useState<number | undefined>()
  const controlledContext = onContextLengthChange !== undefined
  const contextLength =
    (controlledContext ? contextLengthProp || undefined : ownContextLength) ??
    undefined
  const setContextLength = (value: number) =>
    controlledContext
      ? onContextLengthChange(value)
      : setOwnContextLength(value)
  const choices = contextChoices(declared, contextLength)
  const [concurrency, setConcurrency] = useState(1)
  const [quantization, setQuantization] = useState("")
  const [everything, setEverything] = useState(false)

  /**
   * What a serving block built from this row should declare, which is not what the
   * matrix sized at. Sizing falls back to fp16 for weights that ship unquantized,
   * because bytes-per-param needs a number; the block must not carry that fallback,
   * because
   * it becomes `--quantization` and fp16 is a dtype no engine accepts there. So the
   * footprint takes the explicit choice, then what the repo actually ships, then
   * nothing — and nothing is the right answer for unquantized weights.
   */
  const declaredQuantization =
    quantization || ((artifact.metadata.quantization as string | null) ?? "")

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
          <p className="font-mono text-2xs text-muted-foreground">
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
          <span className="ml-auto font-mono text-2xs text-muted-foreground">
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
          <p className="font-mono text-2xs text-muted-foreground">
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
        <p className="font-mono text-2xs text-muted-foreground">{data.note}</p>
      )}
      {data?.sizable && data.items.length > 0 && data.note && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2.5">
          <p className="font-mono text-2xs text-warning">{data.note}</p>
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
              <tr className="border-b font-mono text-2xs tracking-wider text-muted-foreground uppercase">
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
                  quantization={declaredQuantization}
                  onPick={onPick}
                  selecting={selecting}
                  selected={
                    selected?.device_class === fit.device_class
                      ? selected
                      : null
                  }
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
          className="mt-2 font-mono text-2xs text-muted-foreground hover:text-primary"
        >
          showing the top {data.items.length} of {data.considered} ranked — show
          all
        </button>
      )}
      {data?.sizable && everything && data.items.length > 5 && (
        <button
          type="button"
          onClick={() => setEverything(false)}
          className="mt-2 font-mono text-2xs text-muted-foreground hover:text-primary"
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

/**
 * How many cards of the chosen class, when more than one count serves.
 *
 * The row's own numbers are the fewest cards that carry the full window, which is one
 * answer to a question that has several. A node of H200 will hold a 70B on two cards
 * and hold it better on eight, and a 120B at 32k on four rather than at 131k on eight —
 * those are trades between context, throughput and what is left of the node for
 * somebody else, and none of them is the plane's to make. Offering only the cheapest
 * one is what made a whole node look like the only thing on the menu.
 *
 * Under the selected row rather than in a column, because it is a second question and
 * it is only worth asking once the class is settled. A layout the fleet is too small
 * for is shown and not offered, for the same reason a short row is: waiting does not
 * turn four cards into eight.
 */
function LayoutChoices({
  fit,
  chosen,
  onPick,
}: {
  fit: ArtifactFit
  /** Device count of the picked layout, so the strip says which one is live. */
  chosen: number
  onPick: (layout: ArtifactFitLayout) => void
}) {
  return (
    <div className="space-y-1.5">
      <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
        How many
      </span>
      <div className="flex flex-wrap gap-1.5">
        {fit.layouts.map((layout) => {
          const picked = layout.device_count === chosen
          const perNode = Math.min(fit.devices_per_node, layout.device_count)
          return (
            <Tooltip key={layout.device_count}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  disabled={layout.supply_short}
                  onClick={() => onPick(layout)}
                  className={cn(
                    "flex flex-col items-start rounded-md border px-2 py-1 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                    picked
                      ? "border-primary bg-primary/10"
                      : "border-border hover:border-primary/50 hover:bg-primary/5",
                    layout.supply_short &&
                      "hover:border-border hover:bg-transparent"
                  )}
                >
                  <span className="font-mono text-xs tabular-nums">
                    {layout.node_count > 1
                      ? `${layout.node_count} nodes x ${perNode}`
                      : `${layout.device_count} dev`}
                  </span>
                  <span className="font-mono text-2xs text-muted-foreground tabular-nums">
                    tp{layout.tensor_parallel}
                    {layout.pipeline_parallel > 1 &&
                      `/pp${layout.pipeline_parallel}`}
                    {layout.context_length > 0 && (
                      <>
                        {" · "}
                        <span className={cn(layout.reduced && "text-warning")}>
                          {formatTokens(layout.context_length)}
                        </span>
                      </>
                    )}
                  </span>
                </button>
              </TooltipTrigger>
              <TooltipContent className="max-w-80">
                {layout.reason}
                {layout.device_count === fit.device_count && (
                  <span className="mt-1 block text-muted-foreground">
                    The fewest cards that carry the full window.
                  </span>
                )}
                {layout.supply_short && (
                  <span className="mt-1 block text-warning">
                    The fleet holds {fit.devices_total} of this class and this
                    layout needs {layout.device_count}.
                  </span>
                )}
              </TooltipContent>
            </Tooltip>
          )
        })}
      </div>
      <p className="font-mono text-2xs text-muted-foreground">
        Fewer cards serve a shorter window and leave the rest of the node for
        something else. More buy context and concurrency.
      </p>
    </div>
  )
}

function FitRow({
  fit,
  quantization,
  onPick,
  selecting = false,
  selected = null,
  actionLabel = "Deploy",
}: {
  fit: ArtifactFit
  /** What a block from this row declares — the repo's own, never the sizing fallback. */
  quantization: string
  onPick?: (footprint: ServingFootprint, fit: ArtifactFit) => void
  selecting?: boolean
  /** The chosen footprint when it is this row's class, null when another row holds it. */
  selected?: ServingFootprint | null
  actionLabel?: string
}) {
  const state = fitTone(fit)
  /** The row's own numbers unless a layout says otherwise, which is what the strip does. */
  const pick = (layout?: ArtifactFitLayout) =>
    onPick?.(
      {
        device_class: fit.device_class,
        device_count: layout?.device_count ?? fit.device_count,
        tensor_parallel: layout?.tensor_parallel ?? fit.tensor_parallel,
        pipeline_parallel: layout?.pipeline_parallel ?? fit.pipeline_parallel,
        context_length: layout?.context_length ?? fit.context_length,
        quantization,
      },
      fit
    )
  /**
   * The whole row is the target when selecting. A button in the last cell is right for
   * an action — it is a thing you do — but wrong for a choice among five, where the
   * clickable area should be the thing you are comparing.
   */
  /**
   * Every row is choosable, including the ones the sizing does not like.
   *
   * Gating on `fits` would make an estimate the final word on what an operator is
   * allowed to run. It is not the final word and it is not always right: a model with
   * sliding-window layers can be charged a full window on layers that hold a thousand
   * tokens, and every row that would have served it would be greyed out. The failure modes are not symmetric: a shape the estimate
   * was wrong to allow is a worker that OOMs and says so, and a shape it was wrong to
   * refuse is a model nobody can deploy and no way to find out why.
   *
   * `possible` is where the line actually is, and the plane draws it in the same place:
   * the weights sitting on the cards and the heads dividing across the tensor group are
   * arithmetic, the plane's `check_capacity` refuses over exactly those, and offering a
   * row it will refuse is a dead end rather than a choice. Everything past that line —
   * the window, the batch, the KV figure — is an estimate against a flat envelope, and
   * the row says so instead of disabling itself: dimmed, with its reason under it, and
   * a warning carried onto the params step.
   */
  const choosable = selecting && fit.possible && !!onPick
  /** A strip of one is not a choice, and a class nobody picked is not asking a question. */
  const choices = selecting && !!selected && fit.layouts.length > 1
  return (
    <>
      <tr
        {...activationProps(choosable ? () => pick() : undefined)}
        aria-selected={selecting ? !!selected : undefined}
        className={cn(
          "border-b last:border-0",
          choosable && ACTIVATION_FOCUS,
          (!fit.fits || fit.supply_short) && "bg-muted/40",
          choosable && "cursor-pointer hover:bg-primary/5",
          selected && "bg-primary/10 hover:bg-primary/10",
          choices && "border-b-0"
        )}
      >
        {selecting && (
          <td className="pl-2">
            <span
              className={cn(
                "flex size-3.5 items-center justify-center rounded-full border",
                selected
                  ? "border-primary bg-primary"
                  : choosable
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
                  Hardware the contract device table does not name. Fitted
                  against the memory a cluster reported for it.
                </TooltipContent>
              </Tooltip>
            )}
          </div>
          <span className="font-mono text-2xs text-muted-foreground">
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
            <TooltipContent className="max-w-80">
              {fit.reason}
              {fit.supply_short && (
                <span className="mt-1 block text-warning">
                  {shortfall(fit)}
                </span>
              )}
            </TooltipContent>
          </Tooltip>
        </td>
        <td className="p-2">
          {fit.fits ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <div className="flex flex-col">
                  <span
                    className={cn(
                      "font-mono text-xs tabular-nums",
                      fit.supply_short && "text-warning"
                    )}
                  >
                    {fit.node_count > 1
                      ? `${fit.node_count} nodes x ${Math.min(fit.devices_per_node, fit.device_count)}`
                      : `1 node x ${fit.device_count}`}
                  </span>
                  <span className="font-mono text-2xs text-muted-foreground tabular-nums">
                    {fit.device_count} dev · tp{fit.tensor_parallel}/pp
                    {fit.pipeline_parallel}
                  </span>
                </div>
              </TooltipTrigger>
              <TooltipContent className="max-w-80">
                {fit.node_count > 1
                  ? `${fit.node_count} nodes of ${Math.min(fit.devices_per_node, fit.device_count)}, on one backend fabric — tensor-parallel ${fit.tensor_parallel} inside each node, pipeline-parallel ${fit.pipeline_parallel} across them.`
                  : `One node of ${fit.device_count}, tensor-parallel ${fit.tensor_parallel}. A node of this class holds ${fit.devices_per_node} devices.`}
                {fit.supply_short && (
                  <span className="mt-1 block text-warning">
                    {shortfall(fit)}
                  </span>
                )}
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
          {/* A row the fleet cannot supply is not offered. Placement would refuse it,
            and the count is a hard bound rather than a busy one — waiting does not
            turn five cards into sixty-four. A row the *sizing* dislikes is offered:
            that is an estimate, and `possible` is the half worth refusing over. */}
          {fit.possible && !fit.supply_short && onPick && !selecting && (
            <Button size="sm" variant="outline" onClick={() => pick()}>
              {actionLabel}
            </Button>
          )}
        </td>
      </tr>
      {choices && (
        <tr className="border-b bg-primary/10 last:border-0">
          <td />
          <td colSpan={6} className="px-2 pb-2.5">
            <LayoutChoices
              fit={fit}
              chosen={selected!.device_count}
              onPick={(layout) => pick(layout)}
            />
          </td>
        </tr>
      )}
    </>
  )
}
