import { MockNotice } from "@/components/workbench/mock-notice"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { WriteAction } from "@/components/layout/write-action"
import { cn } from "@/lib/utils"
import {
  mockProbes,
  tally,
  type ProbeOutcome,
  type ProbeRow,
} from "@/modules/benchmarks/mock"
import { IconChevronRight, IconRefresh } from "@tabler/icons-react"
import { useMemo, useState } from "react"

/**
 * The acceptance ledger: one row per behavioral claim, and whether it matches.
 *
 * This is a different table from every other one in the console and the
 * difference is deliberate. Density is the feature, fifty rows have to be
 * legible without scrolling past the failure, so a row is one line, the
 * verdict is a color in a fixed column, and nothing is padded for air.
 *
 * The three outcomes are drawn as three things, never as pass and not-pass.
 * A probe that could not be observed is the one an acceptance document gets
 * argued over, and an interface that renders it the same as a green row has
 * already lost that argument.
 */

const OUTCOME_STYLE: Record<
  ProbeOutcome,
  { dot: string; text: string; label: string }
> = {
  agrees: {
    dot: "bg-emerald-500",
    text: "text-success",
    label: "agrees",
  },
  differs: {
    dot: "bg-red-500",
    text: "text-destructive",
    label: "differs",
  },
  "not-observed": {
    dot: "bg-muted-foreground/40",
    text: "text-muted-foreground",
    label: "not observed",
  },
}

type Filter = ProbeOutcome | "all"

/**
 * The suite view: a whole conformance run, read as a ledger.
 *
 * `reference` is the endpoint the diff is against. Without one there is no
 * verdict at all, only a record of what was observed, and the interface says so
 * instead of coloring every row green.
 */
export function ConformanceSuite({
  id,
  reference,
}: {
  id: string
  reference?: string | null
}) {
  const rows = useMemo(() => mockProbes(id, !!reference), [id, reference])
  const counts = tally(rows)

  return (
    <div className="space-y-4">
      <MockNotice what="The conformance ledger, laid out against invented probe results." />

      <StatRow>
        <StatTile
          label="Claims checked"
          value={`${counts.total - counts.notObserved} / ${counts.total}`}
        />
        <StatTile
          label="Agrees"
          value={String(counts.agrees)}
          tone={counts.agrees === counts.total ? "ok" : undefined}
        />
        <StatTile
          label="Differs"
          value={String(counts.differs)}
          tone={counts.differs > 0 ? "danger" : undefined}
        />
        <StatTile
          label="Not observed"
          value={String(counts.notObserved)}
          tone={counts.notObserved > 0 ? "warn" : undefined}
        />
        <StatTile
          label="Reference"
          value={reference ?? "none"}
          size="sm"
          tone={reference ? undefined : "warn"}
        />
      </StatRow>

      {!reference && (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-warning">
          No reference endpoint was set, so nothing here is a verdict. These
          rows record what this endpoint answered and nothing about whether that
          is what it should have answered: the standard asks for consistency
          with the original provider, and consistency needs two endpoints.
        </p>
      )}

      <ProbeLedger rows={rows} withReference={!!reference} />
    </div>
  )
}

/** The run view: the same ledger, for one run instead of a whole suite. */
export function ConformanceRun({ id }: { id: string }) {
  const rows = useMemo(() => mockProbes(id), [id])
  const counts = tally(rows)
  return (
    <div className="space-y-4">
      <MockNotice what="One run's probe results, invented." />
      <StatRow>
        <StatTile label="Probes" value={String(counts.total)} />
        <StatTile label="Agrees" value={String(counts.agrees)} tone="ok" />
        <StatTile
          label="Differs"
          value={String(counts.differs)}
          tone={counts.differs > 0 ? "danger" : undefined}
        />
        <StatTile
          label="Not observed"
          value={String(counts.notObserved)}
          tone={counts.notObserved > 0 ? "warn" : undefined}
        />
      </StatRow>
      <ProbeLedger rows={rows} withReference />
    </div>
  )
}

function ProbeLedger({
  rows,
  withReference,
}: {
  rows: ProbeRow[]
  withReference: boolean
}) {
  const [filter, setFilter] = useState<Filter>("all")
  const [open, setOpen] = useState<string | null>(null)

  const shown = rows.filter((row) => filter === "all" || row.outcome === filter)
  const counts = tally(rows)

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="text-sm">Claims</CardTitle>
            <p className="mt-1 text-2xs text-muted-foreground">
              One row per behavioral claim in the standard. Expand a row for the
              bytes that produced it.
            </p>
          </div>
          <div className="flex items-center gap-1">
            <LedgerFilter
              active={filter}
              onChange={setFilter}
              counts={counts}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="px-0">
        <div className="divide-y divide-border/60 border-y">
          {shown.map((row) => (
            <ProbeLine
              key={row.id}
              row={row}
              withReference={withReference}
              expanded={open === row.id}
              onToggle={() => setOpen(open === row.id ? null : row.id)}
            />
          ))}
          {shown.length === 0 && (
            <p className="px-3 py-6 text-center text-xs text-muted-foreground">
              No claim in this suite has that outcome.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function LedgerFilter({
  active,
  onChange,
  counts,
}: {
  active: Filter
  onChange: (filter: Filter) => void
  counts: ReturnType<typeof tally>
}) {
  const entries: [Filter, string, number][] = [
    ["all", "All", counts.total],
    ["differs", "Differs", counts.differs],
    ["not-observed", "Not observed", counts.notObserved],
    ["agrees", "Agrees", counts.agrees],
  ]
  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([value, label, count]) => (
        <button
          key={value}
          type="button"
          onClick={() => onChange(value)}
          className={cn(
            "rounded-md px-2 py-1 font-mono text-2xs tracking-[0.06em] transition-colors",
            active === value
              ? "bg-primary/10 text-primary"
              : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
          )}
        >
          {label} {count}
        </button>
      ))}
    </div>
  )
}

/**
 * One claim, one line.
 *
 * The re-run button is per row because that is the loop the page exists for:
 * read a difference, change something in another system, re-run that one row,
 * read it again. Twenty times in an evening. Re-running the whole suite to
 * check one claim is the behavior this replaces.
 */
function ProbeLine({
  row,
  withReference,
  expanded,
  onToggle,
}: {
  row: ProbeRow
  withReference: boolean
  expanded: boolean
  onToggle: () => void
}) {
  const style = OUTCOME_STYLE[row.outcome]
  return (
    <div>
      <div className="flex items-center gap-2 px-3 py-1.5 hover:bg-muted/40">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <IconChevronRight
            className={cn(
              "size-3 shrink-0 text-muted-foreground transition-transform",
              expanded && "rotate-90"
            )}
          />
          <span className={cn("size-1.5 shrink-0 rounded-full", style.dot)} />
          <Badge
            variant="outline"
            className="w-24 shrink-0 justify-center font-mono text-2xs"
          >
            {row.group}
          </Badge>
          <span className="min-w-0 flex-1 truncate text-xs">{row.claim}</span>
          <span className="hidden w-40 shrink-0 truncate text-right font-mono text-2xs text-muted-foreground sm:block">
            {row.observed}
          </span>
          {withReference && (
            <span className="hidden w-40 shrink-0 truncate text-right font-mono text-2xs text-muted-foreground md:block">
              {row.reference}
            </span>
          )}
          <span
            className={cn(
              "w-24 shrink-0 text-right font-mono text-2xs",
              style.text
            )}
          >
            {style.label}
          </span>
        </button>
        <WriteAction>
          <Button
            size="icon"
            variant="ghost"
            className="size-6 shrink-0"
            title="Re-run this claim"
          >
            <IconRefresh className="size-3" />
          </Button>
        </WriteAction>
      </div>

      {expanded && (
        <div className="space-y-2 bg-muted/30 px-3 py-3 pl-10">
          <div className="grid gap-3 sm:grid-cols-2">
            <Reading label="This endpoint" value={row.observed} />
            {withReference && (
              <Reading label="Reference" value={row.reference ?? "—"} />
            )}
          </div>
          <div>
            <p className="mb-1 font-mono text-2xs tracking-[0.12em] text-muted-foreground uppercase">
              Exchange
            </p>
            <pre className="overflow-x-auto rounded-md border bg-background p-2 font-mono text-2xs leading-relaxed">
              {row.exchange}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

function Reading({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="font-mono text-2xs tracking-[0.12em] text-muted-foreground uppercase">
        {label}
      </p>
      <p className="font-mono text-xs">{value}</p>
    </div>
  )
}
