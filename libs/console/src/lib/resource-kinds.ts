import {
  IconChartHistogram,
  IconActivity,
  IconBell,
  IconBuildingBank,
  IconBolt,
  IconBox,
  IconCpu,
  IconDeviceDesktopAnalytics,
  IconFolders,
  IconGauge,
  IconLayoutDashboard,
  IconLayoutGrid,
  IconPackages,
  IconRadar2,
  IconRocket,
  IconServer,
  IconServer2,
  IconShieldLock,
  IconSortDescending,
  IconStack2,
  IconTimelineEvent,
  IconTopologyStar3,
} from "@tabler/icons-react"

export type ResourceKind =
  | "overview"
  | "cluster"
  | "node"
  | "device"
  | "device-class"
  | "pool"
  | "topology-domain"
  | "artifact"
  | "workload"
  | "deployment"
  | "endpoint"
  | "tenant"
  | "project"
  | "quota"
  | "priority-class"
  | "policy"
  | "operation"
  | "benchmark"
  | "allocation"
  | "event"
  | "discovered"
  | "alert"

/**
 * One icon per kind of thing, resolved in one place. A node is the same glyph in
 * the sidebar, the page header, a card and a table cell: an operator learns the
 * vocabulary once.
 */
export const RESOURCE_ICONS: Record<ResourceKind, React.ElementType> = {
  overview: IconLayoutDashboard,
  cluster: IconServer2,
  node: IconServer,
  device: IconCpu,
  "device-class": IconDeviceDesktopAnalytics,
  pool: IconStack2,
  "topology-domain": IconTopologyStar3,
  artifact: IconPackages,
  workload: IconRocket,
  deployment: IconBox,
  endpoint: IconBolt,
  tenant: IconBuildingBank,
  project: IconFolders,
  quota: IconGauge,
  "priority-class": IconSortDescending,
  policy: IconShieldLock,
  operation: IconActivity,
  benchmark: IconChartHistogram,
  allocation: IconLayoutGrid,
  event: IconTimelineEvent,
  discovered: IconRadar2,
  alert: IconBell,
}
