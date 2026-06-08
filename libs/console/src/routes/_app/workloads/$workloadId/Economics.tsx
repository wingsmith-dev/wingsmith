import {
  DataTable,
  type Column,
  type ListState,
} from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { TabsContent, TabsTrigger } from "@/components/ui/tabs"
import { formatCurrency, formatCurrencyCompact } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useServingBenchmarkSummary } from "@/modules/benchmarks/queries"
import {
  DAY_POINTS,
  DEFAULT_PREFILL_MULTIPLE,
  HOURS_PER_POINT,
  economics,
  type Sensitivity,
} from "@/modules/economics/model"
import {
  toInputs,
  useEconomicsForm,
  type EconomicsSeed,
} from "@/modules/economics/store"
import { useWorkloadPlacement } from "@/modules/demand/queries"
import { useEconomics } from "@/modules/settings/store"
import type { Workload } from "@/modules/demand/types"
import { IconCoin } from "@tabler/icons-react"
import { useMemo, useRef } from "react"

/**
 * The tab itself, and the view behind it, each drawing nothing while the flag is
 * off — not disabled, not empty, absent.
 *
 * They are two components rather than two conditionals in `page.tsx` because
 * that file is 2,400 lines and another workstream is holding it: the flag, the
 * icon and the whole view arrive there as one import and two tags. The trigger
 * mirrors `page.tsx`'s own `ViewTab` exactly, which is a local helper there and
 * not exported.
 */
export function EconomicsTab() {
  if (!useEconomics()) return null
  return (
    <TabsTrigger value="economics">
      <IconCoin />
      Economics
    </TabsTrigger>
  )
}

export function EconomicsTabContent({ workload }: { workload: Workload }) {
  if (!useEconomics()) return null
  return (
    <TabsContent value="economics" className="space-y-4">
      <EconomicsView workload={workload} />
    </TabsContent>
  )
}

/**
 * What this workload costs, what it earns, and how much traffic it needs.
 *
 * Behind a flag, and not because the numbers are secret from us: they are the
 * one view on this page that must not survive being handed to a customer. See
 * `useEconomics`.
 *
 * Every figure is computed in this browser. The plane records no price and no
 * cost — it has never been asked to — so the only things here that came from it
 * are the three facts it does hold: how many cards placement gave this workload,
 * how many replicas it runs, and what a graded benchmark of this exact serving
 * configuration measured. Everything else is the operator's model, kept in
 * `localStorage` under this workload's id and written back nowhere.
 *
 * The layout follows the one thing that makes this hard. Cost is fixed per hour
 * and revenue is variable, so margin is not a number, it is an integral over a
 * day of demand. The demand curve is therefore the first panel and not the last,
 * and the headline is break-even utilization rather than margin percent.
 */
function EconomicsView({ workload }: { workload: Workload }) {
  const placement = useWorkloadPlacement(workload.id)
  const measured = useServingBenchmarkSummary(workload.serving_digest)

  const replicas = workload.scale.desired || workload.scale.placed || 1
  const placed = workload.scale.placed || replicas
  const seed = useMemo<EconomicsSeed>(
    () => ({
      measuredOutputTokensPerSecond:
        measured.data?.peak_output_tokens_per_second ?? null,
      replicas,
      gpusPerReplica: (placement.data?.gpu_count ?? 0) / Math.max(placed, 1),
    }),
    [
      measured.data?.peak_output_tokens_per_second,
      replicas,
      placement.data?.gpu_count,
      placed,
    ]
  )

  const { form, saved, set, merge, setUtilizationPoint, reset } =
    useEconomicsForm(workload.id, seed)
  const model = useMemo(() => economics(toInputs(form)), [form])

  const noCapacity = form.outputTokensPerSecond <= 0
  const breakEvenTile = model.breakEven.flatUtilization

  return (
    <div className="space-y-4">
      <Provenance
        measured={form.capacitySource === "measured"}
        saved={saved}
        onReset={reset}
      />

      <StatRow>
        <StatTile
          label="Break-even utilization"
          value={
            breakEvenTile === null
              ? "—"
              : breakEvenTile > 1
                ? `${percent(breakEvenTile)} · above capacity`
                : percent(breakEvenTile)
          }
          tone={
            breakEvenTile === null || breakEvenTile > 1
              ? "warn"
              : breakEvenTile < model.day.meanServed
                ? "ok"
                : "warn"
          }
          small={breakEvenTile !== null && breakEvenTile > 1}
        />
        <StatTile
          label="Gross margin / day"
          value={formatCurrency(model.day.marginPerDay)}
          tone={model.day.marginPerDay >= 0 ? "ok" : "warn"}
        />
        <StatTile
          label="Gross margin"
          value={
            model.day.marginRatio === null
              ? "—"
              : percent(model.day.marginRatio)
          }
          tone={
            model.day.marginRatio !== null && model.day.marginRatio >= 0
              ? "ok"
              : "warn"
          }
        />
        <StatTile
          label="Cost / M output tokens"
          value={formatCurrency(model.day.costPerMOutputTokens, true)}
        />
        <StatTile
          label="Margin / month"
          value={formatCurrencyCompact(model.marginPerMonth)}
          tone={model.marginPerMonth >= 0 ? "ok" : "warn"}
        />
      </StatRow>

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <div className="flex min-w-0 flex-col gap-4">
          <Panel
            title="A day of demand"
            subtitle="Offered load as a share of capacity, every two hours. Drag a bar, or type below."
          >
            <ChartLegend
              items={[
                {
                  tone: "primary",
                  label: "Served",
                  value: percent(model.day.meanServed),
                },
                {
                  tone: "warn",
                  label: "Offered",
                  value: percent(model.day.meanOffered),
                },
                {
                  tone: "danger",
                  label: "Turned away",
                  value: formatCurrency(model.day.clippedRevenuePerDay),
                },
              ]}
            />
            <DemandChart
              utilization={form.utilization}
              breakEven={model.breakEven.flatUtilization}
              onChange={setUtilizationPoint}
            />
            <ChartAxis labels={["00", "04", "08", "12", "16", "20", "24"]} />

            <div className="mt-3 grid grid-cols-6 gap-2 sm:grid-cols-12">
              {form.utilization.slice(0, DAY_POINTS).map((value, index) => (
                <label key={index} className="space-y-1">
                  <span className="block text-center font-mono text-2xs text-muted-foreground/60">
                    {String(index * HOURS_PER_POINT).padStart(2, "0")}
                  </span>
                  <Input
                    className="px-1 text-center font-mono tabular-nums"
                    type="number"
                    min={0}
                    step={5}
                    value={Math.round(value * 100)}
                    onChange={(event) =>
                      setUtilizationPoint(
                        index,
                        Number(event.target.value) / 100
                      )
                    }
                  />
                </label>
              ))}
            </div>

            <p className="mt-3 rounded-md border bg-muted/40 px-3 py-2 text-2xs text-muted-foreground">
              A card costs the same idle as saturated, so the average is not the
              answer. A day that sits at 130% for six hours and 10% for eighteen
              averages 50% and earns nothing like a flat 50% day: the peak is
              clipped at capacity and that demand is gone. Anything above the
              ceiling is drawn separately for that reason.
            </p>
          </Panel>

          <Panel
            title="Where the money goes"
            subtitle="Integrated over the curve above, never over its mean"
          >
            <FieldGrid className="sm:grid-cols-3">
              <Field label="Revenue / hour, mean">
                {formatCurrency(model.day.revenuePerDay / 24)}
              </Field>
              <Field label="Revenue / hour, saturated">
                {formatCurrency(model.revenuePerHourAtCapacity)}
              </Field>
              <Field label="Cost / hour">
                {formatCurrency(model.costPerHour)}
              </Field>
              <Field label="Revenue / day">
                {formatCurrency(model.day.revenuePerDay)}
              </Field>
              <Field label="Cost / day">
                {formatCurrency(model.day.costPerDay)}
              </Field>
              <Field label="Margin / day">
                <Signed value={model.day.marginPerDay} />
              </Field>
              <Field label="Revenue / month">
                {formatCurrency(model.revenuePerMonth)}
              </Field>
              <Field label="Cost / month">
                {formatCurrency(model.costPerMonth)}
              </Field>
              <Field label="Margin / month">
                <Signed value={model.marginPerMonth} />
              </Field>
              <Field label="Cost / M output tokens">
                {formatCurrency(model.day.costPerMOutputTokens, true)}
              </Field>
              <Field label="Cost / M tokens billed">
                {formatCurrency(model.day.costPerMBilledTokens, true)}
              </Field>
              <Field label="Revenue turned away / day">
                {formatCurrency(model.day.clippedRevenuePerDay)}
              </Field>
              <Field label="Output tokens / day">
                {tokens(model.day.outputTokensPerDay)}
              </Field>
              <Field label="Input tokens / day">
                {tokens(
                  model.day.freshInputTokensPerDay +
                    model.day.cachedInputTokensPerDay
                )}
              </Field>
              <Field label="Idle GPU-hours / day">
                {model.day.idleGpuHoursPerDay.toFixed(1)} ·{" "}
                {formatCurrency(model.day.idleCostPerDay)}
              </Field>
            </FieldGrid>
            <p className="mt-3 text-2xs text-muted-foreground/70">
              Cost per million output tokens is the figure that tells the story:
              it is fixed cost divided by variable volume, so it collapses as
              utilization rises and runs away to nothing as it falls.
            </p>
          </Panel>

          <Panel
            title="Break-even"
            subtitle="What it takes to stop losing money, asked four ways"
          >
            <FieldGrid className="sm:grid-cols-2">
              <Field label="Flat utilization">
                {model.breakEven.flatUtilization === null
                  ? "Never, at these prices"
                  : percent(model.breakEven.flatUtilization)}
              </Field>
              <Field label="Output tokens / day">
                {model.breakEven.outputTokensPerDay === null
                  ? "Never, at these prices"
                  : tokens(model.breakEven.outputTokensPerDay)}
              </Field>
              <Field label="This curve, scaled by">
                {model.breakEven.curveScale === null
                  ? "No amount of demand"
                  : `${model.breakEven.curveScale.toFixed(2)}x`}
              </Field>
              <Field label="…offering, on average">
                {model.breakEven.curveMeanOffered === null
                  ? "—"
                  : percent(model.breakEven.curveMeanOffered)}
              </Field>
              <Field label="Output price that breaks even">
                {model.breakEven.outputPricePerMTok === null
                  ? "—"
                  : model.breakEven.outputPricePerMTok <= 0
                    ? "Already above water at $0"
                    : `${formatCurrency(model.breakEven.outputPricePerMTok, true)} / M`}
              </Field>
              <Field label="Payback on commitment">
                {model.paybackDays === null
                  ? form.upfrontCommitment > 0
                    ? "Never, at this margin"
                    : "Does not apply — rented, no capex"
                  : `${model.paybackDays.toFixed(0)} days`}
              </Field>
            </FieldGrid>
            <p className="mt-3 text-2xs text-muted-foreground/70">
              The curve is scaled rather than flattened, because the shape is a
              property of who the customers are and volume is the thing that
              changes. The load it must average is higher than the flat answer
              exactly when the peak clips — demand thrown away at midday still
              has to be paid for.
            </p>
          </Panel>

          <Panel
            title="Sensitivity"
            subtitle="A ±10% nudge on one input at a time, and the value at which the day stops paying"
          >
            <DataTable
              bare
              columns={SENSITIVITY_COLUMNS}
              list={still(model.sensitivity)}
              rowKey={(row) => row.key}
              empty="Nothing to vary."
            />
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="Price" subtitle="Per million tokens, as quoted">
            <div className="space-y-3">
              <Money
                label="Output (decode)"
                value={form.outputPricePerMTok}
                onChange={(v) => set("outputPricePerMTok", v ?? 0)}
              />
              <Money
                label="Input (prefill)"
                value={form.inputPricePerMTok}
                onChange={(v) => set("inputPricePerMTok", v ?? 0)}
              />
              <Money
                label="Cached input"
                value={form.cachedInputPricePerMTok}
                onChange={(v) => set("cachedInputPricePerMTok", v ?? 0)}
              />
              <p className="text-2xs text-muted-foreground/70">
                Cached input is priced far below fresh input everywhere in the
                industry and costs almost nothing to serve. That gap is a margin
                lever, not a footnote.
              </p>
              <Money
                label="Reference output / M"
                value={form.referenceOutputPricePerMTok}
                onChange={(v) => set("referenceOutputPricePerMTok", v)}
                optional
              />
              <Money
                label="Reference input / M"
                value={form.referenceInputPricePerMTok}
                onChange={(v) => set("referenceInputPricePerMTok", v)}
                optional
              />
              {model.reference && (
                <FieldGrid className="sm:grid-cols-1">
                  <Field label="Blended, ours vs theirs">
                    {formatCurrency(model.reference.ours, true)} vs{" "}
                    {formatCurrency(model.reference.theirs, true)} / M —{" "}
                    <span
                      className={cn(
                        model.reference.delta <= 0
                          ? "text-emerald-600 dark:text-emerald-400"
                          : "text-amber-600 dark:text-amber-400"
                      )}
                    >
                      {model.reference.delta <= 0 ? "undercut by " : "over by "}
                      {percent(Math.abs(model.reference.delta))}
                    </span>
                  </Field>
                </FieldGrid>
              )}
            </div>
          </Panel>

          <Panel title="Traffic shape" subtitle="What one request looks like">
            <div className="space-y-3">
              <Quantity
                label="Input tokens per output token"
                value={form.inputPerOutputToken}
                step={0.5}
                onChange={(v) => set("inputPerOutputToken", v)}
              />
              <Quantity
                label="Cache hit rate"
                suffix="% of input tokens"
                value={Math.round(form.cacheHitRate * 100)}
                step={5}
                onChange={(v) => set("cacheHitRate", v / 100)}
              />
              <FieldGrid className="sm:grid-cols-1">
                <Field label="Input : output : cached, per output token">
                  {model.unit.freshInput.toFixed(2)} :{" "}
                  <span className="text-foreground">1</span> :{" "}
                  {model.unit.cachedInput.toFixed(2)}
                </Field>
              </FieldGrid>
              <p className="text-2xs text-muted-foreground/70">
                Cached is a share of the input tokens, not a third stream — that
                is how every provider quotes it. The three-way split above is
                the same fact restated, so the ratio is never ambiguous.
              </p>
            </div>
          </Panel>

          <Panel
            title="Capacity"
            subtitle="Decode-bound, with prefill charged against it"
            badge={
              <Badge
                variant={
                  form.capacitySource === "measured" ? "default" : "secondary"
                }
              >
                {form.capacitySource === "measured" ? "measured" : "assumed"}
              </Badge>
            }
          >
            <div className="space-y-3">
              <Quantity
                label="Decode throughput, one replica"
                suffix="output tok/s"
                value={form.outputTokensPerSecond}
                step={100}
                onChange={(v) =>
                  merge({
                    outputTokensPerSecond: v,
                    capacitySource: "assumed",
                  })
                }
              />
              <Quantity
                label="Prefill throughput, one replica"
                suffix="input tok/s"
                value={form.prefillTokensPerSecond}
                step={1000}
                onChange={(v) => set("prefillTokensPerSecond", v)}
              />
              <Quantity
                label="Replicas"
                value={form.replicas}
                step={1}
                onChange={(v) => set("replicas", v)}
              />
              <FieldGrid className="sm:grid-cols-1">
                <Field label="Capacity, all replicas">
                  {Math.round(model.capacity.otePerSecond).toLocaleString()}{" "}
                  output-token-equivalents/s
                </Field>
                <Field label="Prefill tax">
                  {model.capacity.prefillTax.toFixed(3)} output tokens forgone
                  per uncached input token
                </Field>
                <Field label="Sustained output, saturated">
                  {Math.round(
                    model.outputTokensPerSecondAtCapacity
                  ).toLocaleString()}{" "}
                  tok/s at this mix
                </Field>
              </FieldGrid>
              {noCapacity && (
                <p className="rounded-md border bg-muted/40 px-3 py-2 text-2xs text-muted-foreground">
                  No graded benchmark of this serving configuration has reported
                  a peak, so nothing was filled in. Every figure on this page is
                  zero until a throughput is entered or a run is taken.
                </p>
              )}
              <p className="text-2xs text-muted-foreground/70">
                Decode and prefill do not consume a machine at the same rate, so
                one scalar tokens/sec would be wrong. Two measurable rates are
                entered and the second is converted into the first: serving{" "}
                {Math.round(form.prefillTokensPerSecond).toLocaleString()} input
                tok/s occupies the machine that would otherwise decode{" "}
                {Math.round(form.outputTokensPerSecond).toLocaleString()} output
                tok/s. Cached input skips the forward pass and is charged
                nothing.
              </p>
              <p className="text-2xs text-muted-foreground/70">
                A measured figure is{" "}
                <span className="text-foreground">peak</span> throughput — the
                best a sweep ever saw, usually past the latency band you would
                sell. The sustainable number is the knee of the load curve,
                which the Workbench will expose later. Sizing revenue on peak
                overstates it. Prefill defaults to {DEFAULT_PREFILL_MULTIPLE}x
                decode and is assumed until measured.
              </p>
            </div>
          </Panel>

          <Panel title="Cost" subtitle="Spot, because nothing else is bought">
            <div className="space-y-3">
              <FieldGrid className="sm:grid-cols-1">
                <Field label="Cards held, from live placement">
                  {placement.data?.gpu_count ?? 0} across{" "}
                  {placement.data?.node_count ?? 0} node
                  {placement.data?.node_count === 1 ? "" : "s"} ·{" "}
                  {form.gpusPerReplica.toFixed(2)} per replica
                </Field>
              </FieldGrid>
              <Quantity
                label="GPUs per replica"
                value={form.gpusPerReplica}
                step={1}
                onChange={(v) => set("gpusPerReplica", v)}
              />
              <Money
                label="Spot rate / GPU-hour"
                value={form.spotRatePerGpuHour}
                onChange={(v) => set("spotRatePerGpuHour", v ?? 0)}
              />
              <Money
                label="Cost / hour, override"
                value={form.costPerHourOverride}
                onChange={(v) => set("costPerHourOverride", v)}
                optional
              />
              <Money
                label="Upfront commitment"
                value={form.upfrontCommitment}
                onChange={(v) => set("upfrontCommitment", v ?? 0)}
              />
              <FieldGrid className="sm:grid-cols-1">
                <Field label="Cost / hour">
                  {formatCurrency(model.costPerHour)} ={" "}
                  {(form.replicas * form.gpusPerReplica).toFixed(0)} GPU x{" "}
                  {formatCurrency(form.spotRatePerGpuHour)}
                </Field>
              </FieldGrid>
              <p className="text-2xs text-muted-foreground/70">
                Spot only — this outfit prices no on-demand and no capacity
                block, so neither is offered here as an alternative.
                Interruption is not priced either: a reclaim mid-day is a real
                risk this model is silent about, and the honest reading of every
                margin below is "while the instance lives".
              </p>
              <p className="text-2xs text-muted-foreground/70">
                Cards move with replicas, which is what makes changing the
                replica count a real question: capacity and cost both scale.
              </p>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}

/* ---- inputs ---------------------------------------------------------- */

function Quantity({
  label,
  suffix,
  value,
  step,
  onChange,
}: {
  label: string
  suffix?: string
  value: number
  step?: number
  onChange: (value: number) => void
}) {
  return (
    <label className="flex items-center justify-between gap-3">
      <span className="min-w-0 flex-1">
        <span className="block font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
          {label}
        </span>
        {suffix && (
          <span className="block text-2xs text-muted-foreground/50">
            {suffix}
          </span>
        )}
      </span>
      <Input
        className="w-28 shrink-0 text-right font-mono tabular-nums"
        type="number"
        step={step}
        value={String(value)}
        onChange={(event) => {
          const next = Number(event.target.value)
          onChange(Number.isFinite(next) ? next : 0)
        }}
      />
    </label>
  )
}

/**
 * A currency field. `optional` lets it hold null, which is not the same as zero:
 * an unset cost override falls back to the derived one, and an unset reference
 * price hides the comparison rather than claiming a competitor charges nothing.
 */
function Money({
  label,
  value,
  onChange,
  optional,
}: {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  optional?: boolean
}) {
  return (
    <label className="flex items-center justify-between gap-3">
      <span className="min-w-0 flex-1 font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </span>
      <span className="flex shrink-0 items-center gap-1">
        <span className="font-mono text-2xs text-muted-foreground/60">$</span>
        <Input
          className="w-24 text-right font-mono tabular-nums"
          type="number"
          step={0.05}
          placeholder={optional ? "unset" : undefined}
          value={value === null ? "" : String(value)}
          onChange={(event) => {
            const raw = event.target.value
            if (raw === "") return onChange(optional ? null : 0)
            const next = Number(raw)
            onChange(Number.isFinite(next) ? next : 0)
          }}
        />
      </span>
    </label>
  )
}

/* ---- the demand curve ------------------------------------------------ */

const CHART_W = 560
const CHART_H = 140
const BAR_GAP = 6

/**
 * The day's offered load, drawn against the capacity ceiling.
 *
 * The bar is split at the ceiling rather than simply clamped: the part above it
 * is demand the workload turns away, and that is a different fact from a busy
 * hour. Dragging sets a point, because a demand curve is a shape somebody has
 * an intuition about and typing twelve numbers is not how an intuition is
 * entered.
 */
function DemandChart({
  utilization,
  breakEven,
  onChange,
}: {
  utilization: number[]
  breakEven: number | null
  onChange: (index: number, value: number) => void
}) {
  const svg = useRef<SVGSVGElement>(null)
  const peak = Math.max(...utilization.map((v) => (v > 0 ? v : 0)), 1.2)
  const top = Math.max(peak * 1.1, 1.2)
  const y = (value: number) => CHART_H - (value / top) * CHART_H
  const width = CHART_W / DAY_POINTS

  const drag = (event: React.PointerEvent<SVGSVGElement>) => {
    if (event.buttons !== 1) return
    const box = svg.current?.getBoundingClientRect()
    if (!box) return
    const index = Math.min(
      DAY_POINTS - 1,
      Math.max(
        0,
        Math.floor(((event.clientX - box.left) / box.width) * DAY_POINTS)
      )
    )
    const value = (1 - (event.clientY - box.top) / box.height) * top
    onChange(index, Math.max(0, Math.round(value * 100) / 100))
  }

  return (
    <svg
      ref={svg}
      viewBox={`0 0 ${CHART_W} ${CHART_H}`}
      preserveAspectRatio="none"
      className="h-36 w-full cursor-ns-resize touch-none"
      onPointerDown={drag}
      onPointerMove={drag}
    >
      {utilization.slice(0, DAY_POINTS).map((raw, index) => {
        const value = raw > 0 ? raw : 0
        const served = Math.min(value, 1)
        const over = Math.max(value - 1, 0)
        return (
          <g key={index}>
            <rect
              x={index * width + BAR_GAP / 2}
              y={y(served)}
              width={width - BAR_GAP}
              height={CHART_H - y(served)}
              className="fill-primary/70"
            />
            {over > 0 && (
              <rect
                x={index * width + BAR_GAP / 2}
                y={y(value)}
                width={width - BAR_GAP}
                height={y(1) - y(value)}
                className="fill-destructive/50"
              />
            )}
          </g>
        )
      })}
      <line
        x1={0}
        y1={y(1)}
        x2={CHART_W}
        y2={y(1)}
        className="stroke-foreground/40"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
      {breakEven !== null && breakEven <= top && (
        <line
          x1={0}
          y1={y(breakEven)}
          x2={CHART_W}
          y2={y(breakEven)}
          className="stroke-amber-500"
          strokeWidth={1}
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  )
}

/* ---- sensitivity table ----------------------------------------------- */

const SENSITIVITY_COLUMNS: Column<Sensitivity>[] = [
  {
    key: "label",
    header: "Input",
    cell: (row) => <span className="font-medium">{row.label}</span>,
  },
  {
    key: "current",
    header: "Now",
    align: "right",
    cell: (row) => (
      <span className="font-mono tabular-nums">
        {leverValue(row, row.current)}
      </span>
    ),
  },
  {
    key: "down",
    header: "−10%",
    align: "right",
    secondary: true,
    cell: (row) => (
      <span className="font-mono tabular-nums">
        {formatCurrency(row.marginDown)}
      </span>
    ),
  },
  {
    key: "up",
    header: "+10%",
    align: "right",
    secondary: true,
    cell: (row) => (
      <span className="font-mono tabular-nums">
        {formatCurrency(row.marginUp)}
      </span>
    ),
  },
  {
    key: "swing",
    header: "Swing / day",
    align: "right",
    cell: (row) => (
      <span className="font-mono tabular-nums">
        {formatCurrency(row.swing)}
      </span>
    ),
  },
  {
    key: "breakEven",
    header: "Stops paying at",
    align: "right",
    cell: (row) => (
      <span className="font-mono tabular-nums">
        {row.breakEvenValue === null
          ? "never, in range"
          : leverValue(row, row.breakEvenValue)}
      </span>
    ),
  },
  {
    key: "headroom",
    header: "Headroom",
    align: "right",
    cell: (row) =>
      row.headroom === null ? (
        <span className="text-muted-foreground">—</span>
      ) : (
        <span
          className={cn(
            "font-mono tabular-nums",
            Math.abs(row.headroom) < 0.15 &&
              "text-amber-600 dark:text-amber-400"
          )}
        >
          {row.headroom > 0 ? "+" : ""}
          {percent(row.headroom)}
        </span>
      ),
  },
]

function leverValue(row: Sensitivity, value: number): string {
  switch (row.unit) {
    case "currency-mtok":
      return `${formatCurrency(value, true)}/M`
    case "currency-gpu-hour":
      return `${formatCurrency(value, true)}/h`
    case "percent":
      return percent(value)
    case "rate":
      return `${Math.round(value).toLocaleString()} tok/s`
    default:
      return value.toFixed(2)
  }
}

/** A finished list, for the one table on this page that never pages. */
function still<T>(items: T[]): ListState<T> {
  return {
    items,
    loaded: items.length,
    isLoading: false,
    error: null,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: () => {},
  }
}

/* ---- presentation ---------------------------------------------------- */

/**
 * Where these numbers came from, said before any of them are read.
 *
 * Not decoration. Everything below is a model and none of it is a measurement
 * the plane holds, and a currency figure on a control-plane page will be read
 * as a fact unless it is told otherwise.
 */
function Provenance({
  measured,
  saved,
  onReset,
}: {
  measured: boolean
  saved: boolean
  onReset: () => void
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border bg-muted/40 px-3 py-2">
      <p className="text-2xs text-muted-foreground">
        Modelled in this browser, kept in <code>localStorage</code> for this
        workload, and never written to the plane — which records no price and no
        cost. Card count and replica count are live;{" "}
        {measured
          ? "throughput is a measured benchmark peak"
          : "throughput is assumed, not measured"}
        ; every price, rate and demand figure is yours.
      </p>
      <Button size="sm" variant="ghost" disabled={!saved} onClick={onReset}>
        Reset to defaults
      </Button>
    </div>
  )
}

function Signed({ value }: { value: number }) {
  return (
    <span
      className={cn(
        value >= 0
          ? "text-emerald-600 dark:text-emerald-400"
          : "text-amber-600 dark:text-amber-400"
      )}
    >
      {formatCurrency(value)}
    </span>
  )
}

function percent(value: number): string {
  const scaled = value * 100
  return `${Math.abs(scaled) < 10 ? scaled.toFixed(1) : Math.round(scaled)}%`
}

function tokens(value: number): string {
  if (!Number.isFinite(value) || value === 0) return "0"
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`
  return value.toFixed(0)
}

/* ---- local copies of the page's furniture ---------------------------- */
/*
 * Panel, StatRow, StatTile and the chart trim are declared in `page.tsx` and are
 * not exported. They are restated here rather than lifted out, because lifting
 * them means editing a 2,400-line file that another workstream is holding.
 */

function StatRow({ children }: React.PropsWithChildren) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {children}
    </div>
  )
}

function StatTile({
  label,
  value,
  tone,
  small,
}: {
  label: string
  value: string
  tone?: "ok" | "warn"
  small?: boolean
}) {
  return (
    <Card size="sm" className="gap-1.5 px-3">
      <div className="font-mono text-2xs tracking-wider text-muted-foreground/60 uppercase">
        {label}
      </div>
      <div
        className={cn(
          "font-mono leading-none tabular-nums",
          small ? "text-sm" : "text-xl",
          tone === "ok" && "text-emerald-600 dark:text-emerald-400",
          tone === "warn" && "text-amber-600 dark:text-amber-400"
        )}
      >
        {value}
      </div>
    </Card>
  )
}

function Panel({
  title,
  badge,
  subtitle,
  action,
  children,
}: React.PropsWithChildren<{
  title: string
  badge?: React.ReactNode
  subtitle?: string
  action?: React.ReactNode
}>) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm">
              {title}
              {badge}
            </CardTitle>
            {subtitle && (
              <p className="mt-1 text-2xs text-muted-foreground/60">
                {subtitle}
              </p>
            )}
          </div>
          {action}
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

const DOT = {
  primary: "bg-primary",
  warn: "bg-amber-500",
  danger: "bg-destructive",
} as const

function ChartLegend({
  items,
}: {
  items: { tone: keyof typeof DOT; label: string; value: string }[]
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-3.5">
      {items.map((item) => (
        <span
          key={item.label}
          className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground"
        >
          <span className={cn("size-2 rounded-full", DOT[item.tone])} />
          {item.label}{" "}
          <span className="font-mono text-foreground">{item.value}</span>
        </span>
      ))}
    </div>
  )
}

function ChartAxis({ labels }: { labels: string[] }) {
  return (
    <div className="mt-1 flex justify-between font-mono text-2xs text-muted-foreground">
      {labels.map((label, i) => (
        <span key={i}>{label}</span>
      ))}
    </div>
  )
}
