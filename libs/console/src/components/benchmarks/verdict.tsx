import { Dash } from "@/components/layout/mono"
import { StatusLabel } from "@/components/layout/status-dot"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import {
  formatStat,
  metricLabel,
  verdictLabel,
  verdictTone,
} from "@/modules/benchmarks/display"
import type { BenchmarkVerdict } from "@/modules/benchmarks/types"

/**
 * An outcome and the standard it was derived under, and never one without the
 * other.
 *
 * The verdict is not stored anywhere — it is computed at read against a pinned
 * criteria version — so a bare "fail" is a claim nobody can check or reproduce.
 * The version is a required prop rather than an optional one for exactly that
 * reason: there is no call site that is allowed to leave it out.
 */
export function VerdictBadge({
  outcome,
  criteriaVersion,
  className,
}: {
  outcome: BenchmarkVerdict["outcome"]
  criteriaVersion: string
  className?: string
}) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <StatusLabel tone={verdictTone(outcome)}>
        {verdictLabel(outcome)}
      </StatusLabel>
      <span className="font-mono text-2xs text-muted-foreground/60">
        under {criteriaVersion}
      </span>
    </span>
  )
}

/**
 * Every graded assertion, with the number it graded beside the number it graded
 * against.
 *
 * Both are shown because the question asked immediately after a pass is how
 * close it was. The acceptance floor is `0.85 x baseline` and it belongs to the
 * criteria version rather than to any row here, which is why this table quotes
 * the threshold the plane derived rather than recomputing one.
 */
export function VerdictPanel({
  verdict,
  title = "Verdict",
}: {
  verdict: BenchmarkVerdict
  title?: string
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-sm">
          {title}
          <VerdictBadge
            outcome={verdict.outcome}
            criteriaVersion={verdict.criteria_version}
          />
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {verdict.reason && (
          <p className="text-xs text-muted-foreground">{verdict.reason}</p>
        )}

        {verdict.checks.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No check in {verdict.criteria_version} applies to this measurement.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="whitespace-nowrap">Check</TableHead>
                  <TableHead className="whitespace-nowrap">Outcome</TableHead>
                  <TableHead className="text-right whitespace-nowrap">
                    Observed
                  </TableHead>
                  <TableHead className="whitespace-nowrap"> </TableHead>
                  <TableHead className="text-right whitespace-nowrap">
                    Threshold
                  </TableHead>
                  <TableHead className="whitespace-nowrap">Detail</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {verdict.checks.map((check) => (
                  <TableRow key={check.id}>
                    <TableCell className="font-mono text-2xs whitespace-nowrap">
                      {check.label || metricLabel(check.id)}
                    </TableCell>
                    <TableCell>
                      <StatusLabel tone={verdictTone(check.outcome)}>
                        {verdictLabel(check.outcome)}
                      </StatusLabel>
                    </TableCell>
                    <TableCell className="text-right font-mono text-2xs">
                      {formatStat(check.observed ?? undefined)}
                    </TableCell>
                    <TableCell className="text-center font-mono text-2xs text-muted-foreground/60">
                      {check.comparator}
                    </TableCell>
                    <TableCell className="text-right font-mono text-2xs">
                      {formatStat(check.threshold ?? undefined)}
                    </TableCell>
                    <TableCell className="text-2xs text-muted-foreground">
                      <Dash>{check.detail}</Dash>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
