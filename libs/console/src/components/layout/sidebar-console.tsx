import { HangarMark } from "@/components/layout/hangar-mark"
import {
  ResourceIcon,
  type ResourceKind,
} from "@/components/layout/resource-icon"
import { Badge } from "@/components/ui/badge"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from "@/components/ui/sidebar"
import { transport } from "@/lib/api"
import { useSignOut } from "@/modules/auth/session"
import { useAuthStore } from "@/modules/auth/store"
import { useReadOnly } from "@/modules/settings/store"
import {
  IconEye,
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconLogout,
  IconSettings,
} from "@tabler/icons-react"
import { Link, useRouterState } from "@tanstack/react-router"

type NavItem = {
  label: string
  to: string
  kind: ResourceKind
}

const overviewItems: NavItem[] = [
  { label: "Overview", to: "/overview", kind: "overview" },
]

/**
 * Grouped by what an operator came to do, not by the domain model. Supply,
 * demand, binding and machinery are the right vocabulary in the code and the
 * docs; in a sidebar they ask the reader to translate before they can navigate.
 *
 * Ordered by how often it is opened, not by the order things are created in. You
 * register an artifact a handful of times and look at what is running every day,
 * so the operated thing comes before the thing that defines it. "Where do I
 * start" is Overview's problem, and Overview can stop saying it once there is
 * something running, which a sidebar item cannot.
 */
const servingItems: NavItem[] = [
  { label: "Endpoints", to: "/endpoints", kind: "endpoint" },
  { label: "Workloads", to: "/workloads", kind: "workload" },
  { label: "Artifacts", to: "/artifacts", kind: "artifact" },
]

const fleetItems: NavItem[] = [
  { label: "Clusters", to: "/clusters", kind: "cluster" },
  { label: "Nodes", to: "/nodes", kind: "node" },
  { label: "Devices", to: "/devices", kind: "device" },
  { label: "Allocations", to: "/allocations", kind: "allocation" },
]

const accessItems: NavItem[] = [
  { label: "Tenants", to: "/tenants", kind: "tenant" },
  { label: "Projects", to: "/projects", kind: "project" },
  { label: "Quotas", to: "/quotas", kind: "quota" },
  {
    label: "Priority classes",
    to: "/priority-classes",
    kind: "priority-class",
  },
  { label: "Policies", to: "/policies", kind: "policy" },
]

/**
 * What the plane was asked to do and what came back. Workbench sits with
 * Operations and Events rather than beside the serving objects: a run is
 * imperative and has a lifecycle of its own, which is the thing these three
 * have in common and the thing an object list does not.
 */
const activityItems: NavItem[] = [
  { label: "Workbench", to: "/workbench", kind: "benchmark" },
  { label: "Operations", to: "/operations", kind: "operation" },
  { label: "Events", to: "/events", kind: "event" },
]

const activeClass =
  "data-[active=true]:bg-primary/10 data-[active=true]:text-primary data-[active=true]:font-semibold"

function NavGroup({
  label,
  items,
  pathname,
}: {
  label?: string
  items: NavItem[]
  pathname: string
}) {
  return (
    <SidebarGroup>
      {label && (
        <SidebarGroupLabel className="font-mono text-2xs font-semibold tracking-[0.15em] text-muted-foreground/50 uppercase">
          {label}
        </SidebarGroupLabel>
      )}
      <SidebarMenu>
        {items.map((item) => (
          <SidebarMenuItem key={item.to}>
            <SidebarMenuButton
              asChild
              isActive={pathname.startsWith(item.to)}
              tooltip={item.label}
              className={activeClass}
            >
              <Link to={item.to}>
                <ResourceIcon kind={item.kind} />
                <span>{item.label}</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}

export function ConsoleSidebar({
  ...props
}: React.ComponentProps<typeof Sidebar>) {
  const readOnly = useReadOnly()
  const pathname = useRouterState().location.pathname
  const signOut = useSignOut()
  const operator = useAuthStore((s) => s.operator)

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader className="gap-3 p-4">
        <div className="flex items-center gap-2">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-background">
            <HangarMark className="size-4" />
          </div>
          <span className="truncate text-sm leading-none font-semibold tracking-[0.14em] uppercase group-data-[collapsible=icon]:hidden">
            HANGAR
          </span>
        </div>
        {transport.kind === "fixtures" && (
          <Badge
            variant="outline"
            className="justify-center border-amber-500/30 bg-amber-500/10 font-mono text-2xs tracking-[0.18em] text-amber-600 uppercase group-data-[collapsible=icon]:hidden dark:text-amber-400"
          >
            Fixtures
          </Badge>
        )}
      </SidebarHeader>

      <SidebarContent>
        <NavGroup items={overviewItems} pathname={pathname} />
        <NavGroup label="Serving" items={servingItems} pathname={pathname} />
        <NavGroup label="Fleet" items={fleetItems} pathname={pathname} />
        <NavGroup label="Access" items={accessItems} pathname={pathname} />
        <NavGroup label="Activity" items={activityItems} pathname={pathname} />
      </SidebarContent>

      <SidebarFooter className="p-2">
        {readOnly && <ReadOnlyNotice />}
        <SidebarMenu>
          <CollapseItem />
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              isActive={pathname.startsWith("/settings")}
              tooltip="Settings"
              className={activeClass}
            >
              <Link to="/settings" search={{ section: "gpu-models" }}>
                <IconSettings />
                <span>Settings</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton
              tooltip={operator?.email ?? "Sign out"}
              onClick={signOut}
            >
              <IconLogout />
              <span className="truncate">{operator?.email ?? "Sign out"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}

/**
 * Read-only is a mode, not an event: a banner over every page shouts a fact that
 * never changes. It sits by Settings because that is where it is turned off, and
 * it names its own limit — the hub keeps reconciling regardless of this console.
 */
function ReadOnlyNotice() {
  return (
    <Link
      to="/settings"
      search={{ section: "preferences" }}
      title="Writes are blocked from this console. Connected clusters still reconcile."
      className="mb-1 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-amber-700 transition-colors group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0 hover:bg-amber-500/15 dark:text-amber-400"
    >
      <IconEye className="size-4 shrink-0" />
      <span className="min-w-0 flex-1 truncate font-mono text-2xs tracking-[0.14em] uppercase group-data-[collapsible=icon]:hidden">
        read-only
      </span>
    </Link>
  )
}

/**
 * The width control, in the thing whose width it controls.
 *
 * It used to sit in every page's header, which put a chrome control in front of
 * the page's own name on every route. Here it collapses to an icon with the two
 * items under it and stays reachable at that width.
 *
 * Not rendered on mobile: there the sidebar is a sheet that is either open or
 * shut, `state` means nothing, and the way out is tapping away from it. The
 * page header keeps a trigger for that case, because a control for opening the
 * sheet cannot live inside the sheet.
 */
function CollapseItem() {
  const { state, isMobile, toggleSidebar } = useSidebar()
  if (isMobile) return null
  const collapsed = state === "collapsed"
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        tooltip={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        onClick={toggleSidebar}
      >
        {collapsed ? (
          <IconLayoutSidebarLeftExpand />
        ) : (
          <IconLayoutSidebarLeftCollapse />
        )}
        <span>Collapse</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}
