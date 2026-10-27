import { Field, FieldGrid } from "@/components/layout/field"
import { StatusLabel, type StatusTone } from "@/components/layout/status-dot"
import { Card } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatError } from "@/lib/format"
import { cn } from "@/lib/utils"
import { formatMetric } from "@/modules/metrics/format"
import { useEndpointSla } from "@/modules/metrics/queries"
import {
  budgetReading,
  compactCount,
  objectivePercent,
  slaMonths,
  slaPercent,
  slaVerdict,
  type SlaMonth,
  type SlaTone,
} from "@/modules/metrics/sla"
import type { SlaPercentiles, SlaReport } from "@/modules/metrics/types"
import { useMemo, useState } from "react"

const STATUS: Record<SlaTone, StatusTone> = {
  default: "neutral",
  ok: "ok",
  warn: "warn",
  danger: "danger",
}

const BAR: Record<SlaTone, string> = {
  default: "bg-muted-foreground/30",
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  danger: "bg-destructive",
}

/** Which month the card reports, as the window toggle beside the charts does. */
function MonthToggle({
  months,
  value,
  onChange,
}: {
  months: SlaMonth[]
  value: string
  onChange: (month: string) => void
}) {
  return (
    <div
      role="group"
      aria-label="Report month"
      className="inline-flex items-center gap-0.5 rounded-md border bg-muted/40 p-0.5"
    >
      {months.map((month) => (
        <button
          key={month.month}
          type="button"
          aria-pressed={month.month === value}
          onClick={() => onChange(month.month)}
          className={cn(
            "rounded-[3px] px-2 py-0.5 font-mono text-2xs text-muted-foreground transition-colors",
            "hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            month.month === value && "bg-background text-foreground shadow-xs"
          )}
        >
          {month.label}
        </button>
      ))}
    </div>
  )
}

/** Latency and time to first token at the three percentiles, as one small table. */
function Tails({ report }: { report: SlaReport }) {
  const rows: [string, SlaPercentiles][] = [
    ["Latency", report.latency],
    ["TTFT", report.ttft],
  ]
  return (
    <table className="w-full font-mono text-xs tabular-nums">
      <thead>
        <tr className="text-2xs tracking-wider text-muted-foreground uppercase">
          <th scope="col" className="pb-1 text-left font-normal">
            <span className="sr-only">Measurement</span>
          </th>
          {["p50", "p95", "p99"].map((p) => (
            <th key={p} scope="col" className="pb-1 text-right font-normal">
              {p}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map(([label, p]) => (
          <tr key={label}>
            <th
              scope="row"
              className="py-0.5 text-left text-2xs font-normal tracking-wider text-muted-foreground uppercase"
            >
              {label}
            </th>
            {[p.p50, p.p95, p.p99].map((value, i) => (
              <td key={i} className="py-0.5 text-right">
                {formatMetric(value, "seconds")}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SlaBody({ report }: { report: SlaReport }) {
  const verdict = slaVerdict(report)
  const budget = budgetReading(report.error_budget_consumed)
  const objective = objectivePercent(report)

  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-xl leading-none tabular-nums">
          {slaPercent(report.availability)}
        </span>
        <span className="font-mono text-2xs text-muted-foreground">
          {objective === null
            ? "no objective declared"
            : `objective ${slaPercent(objective)}`}
        </span>
      </div>

      <StatusLabel tone={STATUS[verdict.tone]}>{verdict.label}</StatusLabel>

      {report.error_budget_consumed !== null && (
        <div className="space-y-1">
          <div className="flex items-baseline justify-between gap-2 font-mono text-2xs">
            <span className="tracking-wider text-muted-foreground uppercase">
              Error budget
            </span>
            <span className="tabular-nums">{budget.label}</span>
          </div>
          <div
            role="meter"
            aria-label="Error budget used"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(budget.width)}
            aria-valuetext={budget.label}
            className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
          >
            <div
              className={cn("h-full rounded-full", BAR[budget.tone])}
              style={{ width: `${budget.width}%` }}
            />
          </div>
        </div>
      )}

      <div className="space-y-3 border-t pt-3">
        <FieldGrid className="grid-cols-2">
          <Field label="Requests">{compactCount(report.requests)}</Field>
          <Field label="Failed">{slaPercent(report.failure_ratio)}</Field>
        </FieldGrid>
        <Tails report={report} />
      </div>

      <p
        className="font-mono text-2xs text-muted-foreground"
        title={report.definition_text}
      >
        {report.partial ? "month to date · " : ""}
        live traffic at the front door · {report.definition}
      </p>
    </>
  )
}

/**
 * The endpoint's service level for a calendar month, against what its quota
 * promised.
 *
 * Read from `/metrics/endpoints/{ref}/sla`, which judges the month on the
 * plane: this card draws the verdict and never recomputes it. The running month
 * says "on track" or "over budget so far" instead of "met" or "missed",
 * because it is not over.
 */
export function SlaCard({ endpointId }: { endpointId: string }) {
  const months = useMemo(() => slaMonths(new Date()), [])
  const [month, setMonth] = useState(months[0].month)
  const running = months.find((m) => m.month === month)?.running ?? false
  const { data, isLoading, error } = useEndpointSla(endpointId, month, running)

  return (
    <Card size="sm" className="gap-2.5 px-4 py-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-2xs tracking-wider text-muted-foreground uppercase">
          Service level
        </span>
        <MonthToggle months={months} value={month} onChange={setMonth} />
      </div>

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-16" />
        </div>
      ) : error ? (
        <p className="font-mono text-2xs text-destructive">
          could not load the report: {formatError(error)}
        </p>
      ) : data?.unavailable ? (
        <p className="font-mono text-2xs text-muted-foreground">
          {data.unavailable}
        </p>
      ) : data ? (
        <SlaBody report={data} />
      ) : null}
    </Card>
  )
}
