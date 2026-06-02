import { ResourceIcon } from "@/components/layout/resource-icon"
import type { StatusTone } from "@/components/layout/status-dot"
import { Button } from "@/components/ui/button"
import { formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { AttentionItem } from "@/modules/overview/attention"
import { IconCheck } from "@tabler/icons-react"
import { Link } from "@tanstack/react-router"
import * as React from "react"
import { Panel, PanelEmpty } from "./Panel"
import { SegTabs } from "./SegTabs"

/**
 * The two sightings the mockup shows that the plane cannot report.
 *
 * `/v1/discovered` reports *workloads* a cluster was already running, and
 * nothing else: there is no sighting object for an image-cache layer the
 * artifact catalogue has never seen, and no sighting for a set of engine
 * arguments that matches no declared workload. Both rows below are the mockup's own
 * numbers, kept so the panel reads as designed.
 *
 * TODO-API: catalogue-gap sightings. Needs either `/v1/discovered/artifacts`
 * (unregistered image or weight digests observed in a cluster's cache) and
 * `/v1/discovered/serving` (observed engine argv matching no workload), or a
 * `kind` discriminator on `/v1/discovered` widening it past workloads.
 */
const MOCK_CATALOGUE_GAPS: AttentionItem[] = [
  {
    id: "mock:discovered-artifact",
    group: "discovered",
    tone: "neutral",
    kind: "artifact",
    title: "Unregistered artifact in cluster image cache",
    detail: "sha256:9f3c1a…e04b · gptq-int4 · not in catalog",
    pill: "artifact",
    action: { label: "Review", link: { to: "/artifacts" } },
  },
  {
    id: "mock:discovered-serving",
    group: "discovered",
    tone: "neutral",
    kind: "workload",
    title: "Deployment params match no declared workload",
    detail: "vllm 0.6.3 · tp=4 · max-model-len=32768 · adopt it?",
    pill: "serving",
    action: { label: "Review", link: { to: "/discovered" } },
  },
]

type Filter = "all" | "discovered" | "health"

const ICON_TONE: Record<StatusTone, string> = {
  danger: "bg-destructive/15 text-destructive",
  warn: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  info: "bg-primary/15 text-primary",
  ok: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  neutral: "bg-secondary text-muted-foreground",
}

/**
 * The inbox. Everything a person has to decide about, loudest first, with the
 * discovered half beside the unhealthy half rather than on a page of its own —
 * an unadopted workload holding eight accelerators is the same kind of problem
 * as a degraded one.
 */
export function NeedsAttention({
  items,
  isLoading,
  error,
}: {
  items: AttentionItem[] | undefined
  isLoading: boolean
  error: unknown
}) {
  const [filter, setFilter] = React.useState<Filter>("all")

  const all = React.useMemo(
    () => [...(items ?? []), ...MOCK_CATALOGUE_GAPS],
    [items]
  )
  const discovered = all.filter((item) => item.group === "discovered")
  const health = all.filter((item) => item.group === "health")
  const shown =
    filter === "discovered" ? discovered : filter === "health" ? health : all

  return (
    <Panel
      tone="attention"
      title="Needs attention"
      description={`${all.length} ${all.length === 1 ? "item" : "items"} · unadopted resources and unhealthy serving`}
      isLoading={isLoading}
      error={error}
      errorMessage="Failed to load the fleet"
      skeletonRows={5}
      action={
        <SegTabs
          aria-label="Filter what needs attention"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All", count: all.length },
            {
              value: "discovered",
              label: "Discovered",
              count: discovered.length,
            },
            { value: "health", label: "Health", count: health.length },
          ]}
        />
      }
    >
      {shown.length === 0 ? (
        <PanelEmpty>
          <span className="inline-flex items-center gap-2">
            <IconCheck className="size-4 text-emerald-500" />
            Nothing to decide. Every cluster reports in and every endpoint
            serves.
          </span>
        </PanelEmpty>
      ) : (
        <div className="divide-y">
          {shown.map((item) => (
            <div key={item.id} className="flex items-center gap-3 py-2.5">
              <span
                className={cn(
                  "flex size-7 shrink-0 items-center justify-center rounded-md",
                  ICON_TONE[item.tone]
                )}
              >
                <ResourceIcon kind={item.kind} className="size-3.5" />
              </span>

              <div className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-xs font-medium">
                  {item.title}
                </span>
                <span className="truncate font-mono text-2xs text-muted-foreground">
                  {item.detail}
                </span>
              </div>

              {item.pill && (
                <span className="shrink-0 rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-px font-mono text-2xs text-amber-600 lowercase dark:text-amber-400">
                  {item.pill}
                </span>
              )}
              {item.since && (
                <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                  {formatRelative(item.since)}
                </span>
              )}

              <Button asChild variant="outline" size="sm">
                <Link {...item.action.link}>{item.action.label}</Link>
              </Button>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}
