import { benchmarkColumns } from "@/components/benchmarks/benchmark-columns"
import { LoadCurvePanel } from "@/components/benchmarks/curve"
import { DataTable } from "@/components/layout/data-table"
import { Panel } from "@/components/layout/panel"
import { StatRow, StatTile } from "@/components/layout/stat-tile"
import { WriteAction } from "@/components/layout/write-action"
import { Button } from "@/components/ui/button"
import { formatRelative } from "@/lib/format"
import { kneeTile } from "@/modules/benchmarks/display"
import {
  useServingBenchmarkSummary,
  useWorkbenchCurve,
} from "@/modules/benchmarks/queries"
import type { BenchmarkRun } from "@/modules/benchmarks/types"
import type { Workload } from "@/modules/demand/types"
import { staticList, Unbacked } from "./Common"

export function WorkbenchView({
  workload,
  benchmarks,
  curve,
  measured,
  onStartRun,
}: {
  workload: Workload
  benchmarks: BenchmarkRun[] | undefined
  curve: ReturnType<typeof useWorkbenchCurve>
  measured: ReturnType<typeof useServingBenchmarkSummary>
  onStartRun: () => void
}) {
  return (
    <>
      <StatRow>
        <StatTile
          label="Last run"
          value={
            benchmarks?.length
              ? formatRelative(
                  benchmarks[0].started_at ?? benchmarks[0].created_at
                )
              : "—"
          }
        />
        <StatTile
          label="Runs recorded"
          value={String(benchmarks?.length ?? 0)}
        />
        {/*
        The knee of the most recent ramp, derived by the plane at read
        against the pinned criteria version. "not found" and "still
        ramping" are different facts and the tile says which, because a
        knee nobody found is not a knee at zero.
      */}
        <StatTile
          label={
            curve.data ? `Knee · ${curve.data.step_field}` : "Knee concurrency"
          }
          value={kneeTile(curve.data).value}
          size={kneeTile(curve.data).small ? "sm" : "md"}
        />
        {/*
        Rolled up by the plane per `serving_digest`, so it counts every
        graded run of this configuration, including runs taken against a
        different workload that happens to be configured identically. That
        is the point of measuring a configuration instead of a name.
      */}
        <StatTile
          label="Peak throughput tested"
          value={
            measured.data?.peak_output_tokens_per_second != null
              ? `${Math.round(measured.data.peak_output_tokens_per_second).toLocaleString()} tok/s`
              : "—"
          }
          size={
            measured.data?.peak_output_tokens_per_second == null ? "sm" : "md"
          }
        />
        <StatTile
          label="Graded runs, this config"
          value={String(measured.data?.graded_run_count ?? 0)}
        />
      </StatRow>

      <div className="grid gap-4 md:grid-cols-[2fr_1fr]">
        <div className="flex min-w-0 flex-col gap-4">
          <LoadCurvePanel
            curve={curve}
            placed={workload.deployments.length > 0}
            onStartRamp={() => onStartRun()}
          />

          <Panel
            title="Run history"
            description="Every benchmark run taken against this workload's deployments"
          >
            <DataTable
              columns={benchmarkColumns({
                level: "workload",
                showDeployment: true,
                showStep: true,
                showRequest: true,
              })}
              list={staticList(benchmarks ?? [])}
              rowKey={(run) => run.id}
              empty="No benchmark has been run against this workload."
            />
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="New run">
            <p className="text-2xs text-muted-foreground">
              Requests a workload-level measurement of this serving
              configuration. The plane expands the ask into one run per placed
              deployment and grades it against the frozen standard.
            </p>
            <WriteAction>
              <Button size="sm" className="mt-3" onClick={() => onStartRun()}>
                Start run
              </Button>
            </WriteAction>
          </Panel>

          <Unbacked
            title="Engine-argument sweep"
            note="Walking the serving engine's own flags is a different request object to a ramp: a ramp walks one harness parameter, and max_num_seqs, the KV block size and speculative decoding live on the workload's engine args. Nothing on the plane expands that yet, so it shows no numbers instead of invented ones."
          />

          <Unbacked
            title="Headroom before re-tuning"
            note="Would need the live arrival rate at the gateway crossed with the offline knee. The plane computes neither together, and a console that multiplied them would be inventing the answer."
          />
        </div>
      </div>
    </>
  )
}
