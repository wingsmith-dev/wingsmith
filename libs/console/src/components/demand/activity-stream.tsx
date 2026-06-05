import { IconSearch } from "@tabler/icons-react"
import { useMemo, useState } from "react"

import { DeploymentTimeline } from "@/components/demand/deployment-timeline"
import { cn } from "@/lib/utils"
import type { WorkloadDeployment } from "@/modules/demand/types"
import type { PlaneEvent } from "@/modules/machinery/types"

/**
 * The workload's event log, filterable by the object each line is about.
 *
 * This replaced a mocked log pane, and the replacement is not a like-for-like:
 * replica stdout is data-plane and never arrives here. What does arrive is the
 * more useful half for the question this tab is opened to answer — a workload
 * is not running and nobody can say why. Those lines exist, they are written by
 * three different writers, and until the plane learned to expand a workload
 * subject over its children they were unreachable from this page.
 *
 * The object filter is the point of the toolbar. One workload is many
 * deployments and each deployment many replicas, and "which of my clusters is
 * stuck" is answered by narrowing to one of them rather than by reading an
 * interleaved stream. `cache.*` lines are about the weights, which are shared by
 * every deployment on a cluster and so belong to no single one — they are their
 * own bucket rather than being duplicated into each.
 */
export function ActivityStream({
  events,
  loading,
  deployments,
}: {
  events: PlaneEvent[] | undefined
  loading: boolean
  deployments: WorkloadDeployment[]
}) {
  const [subject, setSubject] = useState("all")
  const [severity, setSeverity] = useState<
    "all" | "info" | "warning" | "error"
  >("all")
  const [term, setTerm] = useState("")

  const rows = useMemo(() => {
    const needle = term.trim().toLowerCase()
    return (events ?? []).filter((event) => {
      const isCache = event.kind.startsWith("cache.")
      const matchesSubject =
        subject === "all" ||
        (subject === "weights" && isCache) ||
        (!isCache && event.subject.id === subject) ||
        event.details?.deployment_id === subject
      const matchesSeverity = severity === "all" || event.severity === severity
      const matchesSearch =
        needle === "" ||
        event.message.toLowerCase().includes(needle) ||
        event.kind.toLowerCase().includes(needle)
      return matchesSubject && matchesSeverity && matchesSearch
    })
  }, [events, subject, severity, term])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2.5">
        <select
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          className="h-8 rounded-md border border-input bg-card px-2 font-mono text-2xs"
        >
          <option value="all">Everything</option>
          {deployments.map((d) => (
            <option key={d.id} value={d.id}>
              {d.cluster_name}
            </option>
          ))}
          <option value="weights">Weights</option>
        </select>

        <div className="flex gap-0.5 rounded-md bg-muted p-0.5">
          {(["all", "info", "warning", "error"] as const).map((level) => (
            <button
              key={level}
              type="button"
              onClick={() => setSeverity(level)}
              className={cn(
                "rounded px-2.5 py-1 text-xs capitalize",
                severity === level
                  ? "bg-card text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              {level}
            </button>
          ))}
        </div>

        <label className="relative flex items-center">
          <IconSearch className="pointer-events-none absolute left-2 size-3.5 text-muted-foreground" />
          <input
            type="search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search activity…"
            className="h-8 w-52 rounded-md border border-input bg-card pr-3 pl-7 text-xs placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
        </label>

        <span className="ml-auto font-mono text-2xs text-muted-foreground/60">
          {rows.length} of {events?.length ?? 0}
        </span>
      </div>

      <div className="overflow-y-auto rounded-lg p-3">
        {rows.length === 0 && !loading ? (
          <div className="text-xs text-muted-foreground/60">
            {events?.length
              ? "No activity matches these filters."
              : "Nothing has been recorded against this workload yet."}
          </div>
        ) : (
          <DeploymentTimeline events={rows} loading={loading} />
        )}
      </div>
    </div>
  )
}

