import { Panel } from "@/components/layout/panel"
import { DataTable, type Column } from "@/components/layout/data-table"
import { Field, FieldGrid } from "@/components/layout/field"
import { StatusDot } from "@/components/layout/status-dot"
import { WriteAction } from "@/components/layout/write-action"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  benchmarkPhaseTone,
  CURVE_FIELDS,
  CURVE_LINES,
  curveCeiling,
  curvePath,
  curveSamples,
  curveSegments,
  curveState,
  curveX,
  kneeIndex,
  kneeLabel,
  curveY,
  formatReading,
  stepFieldLabel,
  type CurveAxis,
  type CurveMetric,
  type CurveSample,
  type CurveTone,
} from "@/modules/benchmarks/display"
import type { WorkbenchCurve } from "@/modules/benchmarks/queries"
import {
  curveProgress,
  isOpen,
  reading,
  readingUnit,
  type BenchmarkCurve,
  type BenchmarkCurvePoint,
} from "@/modules/benchmarks/types"
import { IconChartLine } from "@tabler/icons-react"
import { useMemo, useState } from "react"

/**
 * The Workbench's load curve: what a ramp actually measured, and where it bent.
 *
 * Throughput and latency are one trade, so both are drawn against the same
 * steps and share a hover: moving over either chart moves the readout on both,
 * which is what keeps the price of the next unit of load visible, not
 * inferred. They are separate cards because stacking two axes inside one frame
 * made each of them small and neither of them legible, and the trade is read
 * between the cards, not inside a single crowded one.
 *
 * Nothing here is computed from the runs. The curve and the knee are the
 * plane's read, derived against the pinned criteria version and stored nowhere;
 * a console that walked the steps and found its own inflection would be a
 * second, quieter answer to where a configuration saturates.
 *
 * Three absences are drawn three ways and never as a zero. A reading the plane
 * could not resolve is a hole in the line: plotting it at zero would be a lie
 * about the hardware. A step whose run is still open is a pending tick, which
 * is what makes a running ramp fill itself in. A warm-up is a hollow mark
 * joined to nothing, because a discarded run is kept, shown, and counted by
 * nothing.
 */

/* ---- the read --------------------------------------------------------- */

/* ---- the panel -------------------------------------------------------- */

export function LoadCurvePanel({
  curve,
  placed,
  onStartRamp,
}: {
  curve: WorkbenchCurve
  placed: boolean
  onStartRamp: () => void
}) {
  const data = curve.data
  const progress = curveProgress(data)
  const state = curveState(data)

  const action = (
    <WriteAction>
      <Button size="sm" disabled={!placed} onClick={onStartRamp}>
        Run a ramp
      </Button>
    </WriteAction>
  )
  const badge = data ? (
    <Badge variant={progress.open > 0 ? "default" : "outline"}>
      {progress.open > 0
        ? `${progress.done} / ${progress.total} steps`
        : `${progress.total} steps`}
    </Badge>
  ) : undefined

  /*
    Every state that is not a drawn curve is one card, not three. Three empty
    frames describing the same absence is three times the furniture for none of
    the information.
  */
  if (curve.isLoading && curve.request)
    return (
      <Panel title="Latency under load" action={action}>
        <Skeleton className="h-56 w-full" />
      </Panel>
    )
  if (curve.error)
    return (
      <Panel title="Latency under load" action={action}>
        <Note>{formatError(curve.error, "the curve could not be read")}</Note>
      </Panel>
    )
  if (!curve.request)
    return (
      <Panel
        title="Finding the inflection point"
        description="Latency and throughput across a ramp"
        action={action}
      >
        <NeverRamped placed={placed} onStartRamp={onStartRamp} />
      </Panel>
    )
  if (!data || state === "empty")
    return (
      <Panel title="Finding the inflection point" badge={badge} action={action}>
        <Note>
          The ramp has been accepted and has not expanded into any step yet.
        </Note>
      </Panel>
    )
  if (state === "all-failed")
    return (
      <Panel title="Finding the inflection point" badge={badge} action={action}>
        <FailedRamp curve={data} />
      </Panel>
    )

  return (
    <Drawn
      curve={data}
      single={state === "single"}
      action={action}
      badge={badge}
    />
  )
}

function NeverRamped({
  placed,
  onStartRamp,
}: {
  placed: boolean
  onStartRamp: () => void
}) {
  return (
    <div className="flex flex-col items-start gap-3 py-6">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <IconChartLine className="size-4 shrink-0 opacity-60" />
        {placed
          ? "No ramp has been run against this workload. A ramp walks one concurrency at a time and is what a knee is read from."
          : "Nothing to measure yet: placement has not put this workload on a cluster."}
      </div>
      <WriteAction>
        <Button size="sm" disabled={!placed} onClick={onStartRamp}>
          Run a ramp
        </Button>
      </WriteAction>
    </div>
  )
}

/**
 * Every step failed, which is a result and not an empty chart.
 *
 * The steps are still listed: which load the failures started at is most of
 * what a failed ramp has to say.
 */
function FailedRamp({ curve }: { curve: BenchmarkCurve }) {
  return (
    <div className="space-y-3">
      <Note>
        Every step of this ramp finished without reporting. There is no curve to
        draw, and the phases below are what each step ended as.
      </Note>
      <StepTable curve={curve} />
    </div>
  )
}

/* ---- the chart -------------------------------------------------------- */

const W = 560
const PAD_L = 46
const PAD_R = 10
const PLOT_W = W - PAD_L - PAD_R
const PANEL_H = 108
const PLOT_TOP = 16
const AXIS_Y = PLOT_TOP + PANEL_H + 16
const H = AXIS_Y + 6

const STROKE: Record<CurveTone, string> = {
  primary: "stroke-primary",
  warn: "stroke-amber-500",
  danger: "stroke-destructive",
  muted: "stroke-muted-foreground/50",
}

const FILL: Record<CurveTone, string> = {
  primary: "fill-primary",
  warn: "fill-amber-500",
  danger: "fill-destructive",
  muted: "fill-muted-foreground/50",
}

const DOT: Record<CurveTone, string> = {
  primary: "bg-primary",
  warn: "bg-amber-500",
  danger: "bg-destructive",
  muted: "bg-muted-foreground/50",
}

/**
 * A measured ramp, as three cards: one chart each, then the evidence.
 *
 * The hover index lives here, not in either chart, so pointing at a step
 * on one moves the readout on the other. Two charts that each tracked their own
 * pointer would let a reader compare latency at one load against throughput at
 * a different one without noticing.
 */
function Drawn({
  curve,
  single,
  action,
  badge,
}: {
  curve: BenchmarkCurve
  single: boolean
  action: React.ReactNode
  badge: React.ReactNode
}) {
  const points = useMemo(
    () => [...curve.points].sort((a, b) => a.step_index - b.step_index),
    [curve.points]
  )
  const [hover, setHover] = useState<number | null>(null)

  const series = useMemo(
    () =>
      CURVE_LINES.map((metric) => ({
        metric,
        samples: curveSamples(points, metric.id),
      })),
    [points]
  )

  const knee = kneeIndex(curve)
  const focus = hover ?? (knee >= 0 ? knee : null)
  const axis = stepFieldLabel(curve.step_field).toLowerCase()

  const of = (which: CurveAxis) =>
    series.filter((entry) => entry.metric.axis === which)
  const missing = (which: CurveAxis) =>
    of(which).flatMap(({ metric, samples }) =>
      samples.filter((s) => !s.discard && s.value === null && !isOpen(s.phase))
        .length > 0
        ? [metric.label]
        : []
    )

  return (
    <>
      <Panel
        title="Latency under load"
        badge={badge}
        description={`Time to first token, inter-token and end-to-end across a ${axis} ramp, derived under ${curve.criteria_version}`}
        action={action}
      >
        <AxisChart
          curve={curve}
          points={points}
          series={of("latency")}
          label="latency"
          unit="ms"
          focus={focus}
          knee={knee}
          onHover={setHover}
        />
        {single && (
          <Note>
            One step is a point, not a curve. A knee needs a ramp with at least
            two loads either side of it.
          </Note>
        )}
        {missing("latency").length > 0 && (
          <Note>
            A gap in the line is a reading the plane could not resolve, never a
            zero: {missing("latency").join(", ")}.
          </Note>
        )}
        <FieldGrid>
          <Field label="Knee">{kneeLabel(curve)}</Field>
          <Field label={`Method · ${curve.knee.method}`}>
            {curve.knee.reason}
          </Field>
        </FieldGrid>
      </Panel>

      <Panel
        title="Throughput under load"
        description={`Tokens and requests per second across the same ${axis} steps`}
      >
        <AxisChart
          curve={curve}
          points={points}
          series={of("throughput")}
          label="throughput"
          unit="tok/s"
          focus={focus}
          knee={knee}
          onHover={setHover}
        />
        {missing("throughput").length > 0 && (
          <Note>
            A gap in the line is a reading the plane could not resolve, never a
            zero: {missing("throughput").join(", ")}.
          </Note>
        )}
      </Panel>

      <Panel
        title="Steps"
        description="Every step of the ramp, warm-ups included"
      >
        <StepTable curve={curve} />
      </Panel>
    </>
  )
}

/** One axis, drawn on its own steps, reporting its hover to whoever owns it. */
function AxisChart({
  curve,
  points,
  series,
  label,
  unit,
  focus,
  knee,
  onHover,
}: {
  curve: BenchmarkCurve
  points: BenchmarkCurvePoint[]
  series: { metric: CurveMetric; samples: CurveSample[] }[]
  label: string
  unit: string
  focus: number | null
  knee: number
  onHover: (index: number | null) => void
}) {
  const count = points.length
  const ceiling = curveCeiling(series.map((entry) => entry.samples))
  const focused = focus !== null ? points[focus] : undefined

  return (
    <div className="space-y-3">
      <ul className="flex flex-wrap items-center gap-3.5">
        {series.map(({ metric }) => (
          <li
            key={metric.id}
            className="inline-flex items-center gap-1.5 text-2xs text-muted-foreground"
          >
            <span className={cn("size-2 rounded-full", DOT[metric.tone])} />
            {metric.label}
            {metric.display && (
              <span className="text-muted-foreground">display only</span>
            )}
          </li>
        ))}
      </ul>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        role="img"
        aria-label={`${stepFieldLabel(curve.step_field)} against ${label}`}
        onPointerLeave={() => onHover(null)}
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect()
          const x = ((event.clientX - box.left) / box.width) * W - PAD_L
          const step = count > 1 ? PLOT_W / (count - 1) : PLOT_W
          const index = Math.round(x / (step || 1))
          onHover(Math.min(count - 1, Math.max(0, index)))
        }}
      >
        <Plot
          top={PLOT_TOP}
          label={label}
          unit={unit}
          ceiling={ceiling}
          series={series}
          count={count}
        />

        {points.map((point, index) => {
          const x = PAD_L + curveX(index, count, PLOT_W)
          const pending = isOpen(point.phase)
          if (!pending && !point.discard && index !== focus) return null
          return (
            <line
              key={point.run}
              x1={x}
              y1={PLOT_TOP}
              x2={x}
              y2={PLOT_TOP + PANEL_H}
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
              strokeDasharray={pending || point.discard ? "3 3" : undefined}
              className={
                index === focus
                  ? "stroke-foreground/30"
                  : "stroke-muted-foreground/25"
              }
            />
          )
        })}

        {knee >= 0 && (
          <g>
            <line
              x1={PAD_L + curveX(knee, count, PLOT_W)}
              y1={PLOT_TOP - 6}
              x2={PAD_L + curveX(knee, count, PLOT_W)}
              y2={PLOT_TOP + PANEL_H}
              strokeWidth={1.5}
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
              className="stroke-amber-500"
            />
            <text
              x={PAD_L + curveX(knee, count, PLOT_W)}
              y={PLOT_TOP - 10}
              textAnchor="middle"
              className="fill-amber-500 font-mono text-[9px]"
            >
              knee
            </text>
          </g>
        )}

        {points.map((point, index) => (
          <text
            key={point.run}
            x={PAD_L + curveX(index, count, PLOT_W)}
            y={AXIS_Y}
            textAnchor="middle"
            className={cn(
              "font-mono text-[9px]",
              "fill-muted-foreground",
              point.discard && "line-through"
            )}
          >
            {point.step_value ?? "—"}
          </text>
        ))}
      </svg>

      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          {stepFieldLabel(curve.step_field)}
        </span>
        {focused && (
          <span className="flex flex-wrap items-baseline gap-3 text-2xs text-muted-foreground">
            <span className="font-mono text-foreground">
              {curve.step_field} {focused.step_value ?? "—"}
            </span>
            {series.map(({ metric }) => (
              <Readout key={metric.id} point={focused} metric={metric} />
            ))}
          </span>
        )}
      </div>
    </div>
  )
}

/** One plot, drawn against its own axis and nobody else's. */

function Plot({
  top,
  label,
  unit,
  ceiling,
  series,
  count,
}: {
  top: number
  label: string
  unit: string
  ceiling: number | null
  series: { metric: CurveMetric; samples: CurveSample[] }[]
  count: number
}) {
  const ticks = [0, 0.5, 1]
  return (
    <g>
      {ticks.map((fraction) => (
        <g key={fraction}>
          <line
            x1={PAD_L}
            y1={top + PANEL_H * (1 - fraction)}
            x2={W - PAD_R}
            y2={top + PANEL_H * (1 - fraction)}
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
            className="stroke-border"
          />
          {ceiling !== null && (
            <text
              x={PAD_L - 6}
              y={top + PANEL_H * (1 - fraction) + 3}
              textAnchor="end"
              className="fill-muted-foreground font-mono text-[8px]"
            >
              {shortNumber(ceiling * fraction)}
            </text>
          )}
        </g>
      ))}
      <text
        x={PAD_L}
        y={top - 4}
        className="fill-muted-foreground font-mono text-[8px] tracking-wider uppercase"
      >
        {label} · {ceiling === null ? "nothing measured" : unit}
      </text>
      {ceiling !== null &&
        series.map(({ metric, samples }) => (
          <g key={metric.id}>
            {curveSegments(samples).map((segment, index) => (
              <polyline
                key={index}
                points={shift(
                  curvePath(segment, count, PLOT_W, ceiling, PANEL_H),
                  top
                )}
                fill="none"
                strokeWidth={1.75}
                strokeLinecap="round"
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
                className={STROKE[metric.tone]}
              />
            ))}
            {/*
              A warm-up above the ceiling is not drawn: the axis is scaled over
              the counted steps only, so a discarded run twice their size would
              be plotted off the panel and over the plot above it. It stays in
              the step table, which is where a warm-up is read anyway.
            */}
            {samples
              .filter(
                (sample) => sample.value !== null && sample.value <= ceiling
              )
              .map((sample) => (
                <circle
                  key={sample.run}
                  cx={PAD_L + curveX(sample.index, count, PLOT_W)}
                  cy={top + curveY(sample.value as number, ceiling, PANEL_H)}
                  r={sample.discard ? 3 : 2.5}
                  strokeWidth={1.25}
                  vectorEffect="non-scaling-stroke"
                  className={cn(
                    sample.discard
                      ? cn("fill-background", STROKE[metric.tone])
                      : FILL[metric.tone]
                  )}
                />
              ))}
          </g>
        ))}
    </g>
  )
}

/** A polyline built at plot-local coordinates, moved down to where it is drawn. */
function shift(path: string, top: number): string {
  return path
    .split(" ")
    .map((pair) => {
      const [x, y] = pair.split(",")
      return `${PAD_L + Number(x)},${top + Number(y)}`
    })
    .join(" ")
}

function shortNumber(value: number): string {
  if (value >= 1000)
    return `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}k`
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}

function Readout({
  point,
  metric,
}: {
  point: BenchmarkCurvePoint
  metric: CurveMetric
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("size-1.5 rounded-full", DOT[metric.tone])} />
      {metric.label}{" "}
      <span className="font-mono text-foreground">
        {formatReading(
          reading(point, metric.id),
          readingUnit(point, metric.id)
        )}
      </span>
    </span>
  )
}

/* ---- the steps as text ------------------------------------------------ */

/**
 * Every step, spelled out.
 *
 * The chart is the shape and this is the evidence: a gap in a line and a run
 * that failed look the same from a distance, and the phase column is what tells
 * them apart. A warm-up is here too, marked, because a discarded run is
 * something to see and nothing to count.
 */
function StepTable({ curve }: { curve: BenchmarkCurve }) {
  const points = [...curve.points].sort((a, b) => a.step_index - b.step_index)
  const columns: Column<BenchmarkCurvePoint>[] = [
    {
      key: "step",
      header: stepFieldLabel(curve.step_field),
      cell: (point) => (
        <span className="font-mono text-xs tabular-nums">
          {point.step_value ?? "—"}
          {point.discard && (
            <span className="ml-2 text-2xs text-muted-foreground">warm-up</span>
          )}
        </span>
      ),
    },
    {
      key: "phase",
      header: "Phase",
      cell: (point) => (
        <span className="inline-flex items-center gap-2">
          <StatusDot tone={benchmarkPhaseTone(point.phase)} />
          <span className="text-2xs text-muted-foreground">{point.phase}</span>
        </span>
      ),
    },
    ...CURVE_FIELDS.filter((metric) => !metric.display).map((metric) => ({
      key: metric.id,
      header: metric.label,
      align: "right" as const,
      cell: (point: BenchmarkCurvePoint) => (
        <span className="font-mono text-xs tabular-nums">
          {formatReading(
            reading(point, metric.id),
            readingUnit(point, metric.id)
          )}
        </span>
      ),
    })),
  ]
  return (
    <DataTable
      columns={columns}
      list={{
        items: points,
        loaded: points.length,
        isLoading: false,
        error: null,
        hasNextPage: false,
        isFetchingNextPage: false,
        fetchNextPage: () => {},
      }}
      rowKey={(point) => point.run}
      empty="This request expanded into no steps."
    />
  )
}

function Note({ children }: React.PropsWithChildren) {
  return <p className="py-2 text-2xs text-muted-foreground">{children}</p>
}
